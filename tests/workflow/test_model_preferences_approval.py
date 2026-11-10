"""Dispatch-boundary refresh: changed settings need approval, active calls keep theirs."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from orch.cli import context
from orch.config import Config, effective_feature
from orch.contracts import (
    ContractError,
    FeatureSpec,
    ModelSpec,
    RoleModels,
    RunState,
    approval_hashes,
)
from orch.git_ops import git
from orch.store import StateStore
from orch.workflow import ApprovalRequired, Engine


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    return repo


@pytest.fixture
def boundary(tmp_path, plan_factory):
    plan, manifest = plan_factory()
    repo = make_repo(tmp_path)
    store = StateStore(tmp_path / "state")
    feature = FeatureSpec(
        slug="demo",
        target_project=repo,
        requirement=plan / "requirement.md",
        models=RoleModels(
            worker=ModelSpec(model="provider/worker", reasoning="low"),
            replacement=RoleModels().replacement,
        ),
    )
    feature = effective_feature(feature, Config(), plan / "feature.md")
    state = RunState(
        run_id="demo-run",
        slug="demo",
        phase="RUNNING",
        stage="WORK",
        approved_hashes=approval_hashes(plan, manifest, feature),
        workspace={
            "repo": str(repo),
            "feature_path": str(tmp_path / "checkout"),
            "feature_branch": "feature/demo",
            "base_sha": "0" * 40,
            "workers": {},
        },
    )
    state = store.save(state)
    return SimpleNamespace(tmp_path=tmp_path, plan=plan, manifest=manifest, repo=repo,
                           store=store, feature=feature, state=state)


def engine_for(boundary, *, feature=None, loader=None, adapters=None):
    return Engine(boundary.store, boundary.state, feature or boundary.feature,
                  boundary.manifest, boundary.plan, adapters=adapters or {},
                  settings_loader=loader)


def test_direct_callers_keep_cached_settings_and_approval(boundary):
    engine = engine_for(boundary)
    engine.check_approval()
    assert boundary.state.phase == "RUNNING"


def test_changed_effective_settings_stop_before_dispatch(boundary):
    changed = boundary.feature.model_copy(deep=True)
    changed.models.worker = ModelSpec(model="provider/other", reasoning="high")
    engine = engine_for(boundary, feature=changed)
    with pytest.raises(ApprovalRequired):
        engine.check_approval()
    assert boundary.state.phase == "PLAN_READY"
    assert "approval_invalidated" in [event["kind"] for event in
                                      boundary.store.events(boundary.state.run_id)]
    # The invocation keeps the settings it started with; nothing is hot-swapped.
    assert engine.feature.models.worker.model == "provider/other"


class FakeAdapter:
    def __init__(self):
        self.calls = []

    async def run(self, request, on_event=None):
        self.calls.append(request)
        (request.output_file.parent).mkdir(parents=True, exist_ok=True)
        request.output_file.write_text("done\n")
        return SimpleNamespace(exit_code=0, duration_seconds=0.01, reason=None,
                               usage=None, adapter_version="fake-1")


def test_loader_change_blocks_dispatch_before_any_agent_call(boundary):
    changed = boundary.feature.model_copy(deep=True)
    changed.models.worker = ModelSpec(model="provider/other", reasoning="high")
    adapter = FakeAdapter()

    def loader():
        return changed, Config()

    engine = engine_for(boundary, loader=loader,
                        adapters={"opencode": adapter, "planner": adapter})
    with pytest.raises(ApprovalRequired):
        asyncio.run(engine.dispatch(
            "worker", "w1", boundary.plan / "workers/w1.md",
            boundary.tmp_path / "out.md", boundary.tmp_path))
    assert adapter.calls == []


def test_shadowed_configuration_edit_keeps_the_effective_hash(boundary):
    pinned = boundary.feature.model_copy(deep=True)
    pinned.models.worker = ModelSpec(model="feature/coder", reasoning="low")

    def loader():
        # A repository edit is fully shadowed by the explicit feature model, so
        # the effective feature read at the boundary is unchanged.
        return pinned, Config(models=RoleModels(
            worker=ModelSpec(model="global/other", reasoning="high")))

    engine = Engine(boundary.store, boundary.state, pinned, boundary.manifest,
                    boundary.plan, adapters={}, settings_loader=loader)
    boundary.state.approved_hashes = approval_hashes(
        boundary.plan, boundary.manifest, pinned)
    boundary.store.save(boundary.state)
    engine.check_approval()


def test_new_unconsented_fallback_stops_before_dispatch(boundary):
    def loader():
        raise ApprovalRequired("Configuration now requires a fresh fallback decision")

    engine = engine_for(boundary, loader=loader)
    with pytest.raises(ApprovalRequired, match="fresh fallback"):
        engine.check_approval()
    assert boundary.state.phase == "RUNNING"


def test_in_flight_call_keeps_its_resolved_model_when_configuration_changes(boundary):
    adapter = FakeAdapter()
    original = boundary.feature.models.worker.model
    changed = boundary.feature.model_copy(deep=True)
    changed.models.worker = ModelSpec(model="provider/other", reasoning="high")
    calls = {"count": 0}

    def loader():
        calls["count"] += 1
        if calls["count"] <= 2:
            return boundary.feature, Config()
        return changed, Config()

    engine = engine_for(boundary, loader=loader,
                        adapters={"opencode": adapter, "planner": adapter})
    with pytest.raises(ApprovalRequired):
        asyncio.run(engine.dispatch(
            "worker", "w1", boundary.plan / "workers/w1.md",
            boundary.tmp_path / "out2.md", boundary.tmp_path))
    assert len(adapter.calls) == 1
    assert adapter.calls[0].model.model == original
    assert engine.feature.models.worker.model == original


def test_lifecycle_fallback_denial_shows_choices_before_any_dispatch(
        boundary, monkeypatch, capsys):
    config_dir = boundary.tmp_path / "config" / "orch"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '[models.worker]\nmodel = "global/coder"\nreasoning = "low"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(boundary.tmp_path / "config"))
    (boundary.repo / ".orch.toml").write_text(
        '[models.worker]\nmodel = "repo/coder"\n')
    features = boundary.store.data_dir / "features"
    features.mkdir(parents=True, exist_ok=True)
    (features / "demo.json").write_text(json.dumps({
        "slug": "demo", "run_id": boundary.state.run_id,
        "repo": str(boundary.repo), "plan": str(boundary.plan),
        "status": "ready"}))

    import orch.models as models_module
    monkeypatch.setattr(models_module, "_interactive", lambda: True)
    shown = {}

    def answer(prompt):
        shown["output"] = capsys.readouterr().out
        return "n"

    monkeypatch.setattr("builtins.input", answer)

    def no_load(run_id):
        raise AssertionError("state must not be loaded before fallback consent")

    monkeypatch.setattr(boundary.store, "load", no_load)
    with pytest.raises(ContractError, match="Declined"):
        context(boundary.store, "demo")
    assert "global/coder" in shown["output"]
    assert "global" in shown["output"]
    assert "worker" in shown["output"]
