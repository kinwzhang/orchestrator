"""Replacement keeps the worker model while allowing its own reasoning setting."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from orch.agents import AgentCapabilities, AgentRequest, OpenCodeAdapter
from orch.config import Config, effective_feature, load_config
from orch.contracts import (
    ContractError,
    FeatureSpec,
    ModelSpec,
    ReplacementModel,
    RoleModels,
    approval_hashes,
    detect_approval_changes,
    read_markdown,
)


def feature_for(tmp_path: Path, models: RoleModels | None = None) -> FeatureSpec:
    return FeatureSpec(
        slug="demo",
        target_project=tmp_path,
        requirement=tmp_path / "requirement.md",
        models=models or RoleModels(),
    )


def test_replacement_inherits_worker_model_with_independent_reasoning():
    models = RoleModels(
        worker=ModelSpec(model="provider/coder", reasoning="low"),
        replacement=ReplacementModel(reasoning="high"),
    )
    replacement = models.resolve("replacement")
    assert replacement.model == "provider/coder"
    assert replacement.reasoning == "high"
    # The ordinary worker keeps its own, lower reasoning setting.
    assert models.resolve("worker").reasoning == "low"


def test_replacement_without_reasoning_preserves_full_worker_inheritance():
    worker = ModelSpec(model="provider/coder", reasoning="low")
    replacement = RoleModels(worker=worker).resolve("replacement")
    assert replacement.model == worker.model
    assert replacement.reasoning == worker.reasoning
    assert replacement == worker
    assert replacement is not worker


def test_resolving_replacement_never_mutates_the_worker_spec():
    worker = ModelSpec(model="provider/coder", reasoning="low")
    models = RoleModels(worker=worker, replacement=ReplacementModel(reasoning="high"))
    first = models.resolve("replacement")
    first.reasoning = "medium"
    assert worker.reasoning == "low"
    assert models.resolve("worker").reasoning == "low"
    assert models.resolve("replacement").reasoning == "high"
    assert models.resolve("replacement") is not worker


def test_replacement_requires_a_configured_worker():
    models = RoleModels(replacement=ReplacementModel(reasoning="high"))
    with pytest.raises(ContractError, match=r"models\.worker\.model"):
        models.resolve("replacement")


def test_replacement_reasoning_follows_global_repo_feature_precedence(tmp_path):
    global_file = tmp_path / "global.toml"
    global_file.write_text(
        '[models.worker]\nmodel = "global/coder"\nreasoning = "low"\n'
        '[models.replacement]\ninherit = "worker"\nreasoning = "high"\n'
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".orch.toml").write_text(
        '[models.replacement]\nreasoning = "medium"\n'
    )
    config = load_config(repo, global_file)
    configured = config.models.resolve("replacement")
    assert configured.model == "global/coder"
    assert configured.reasoning == "medium"

    feature = feature_for(
        tmp_path,
        RoleModels(
            worker=ModelSpec(model="feature/coder"),
            replacement=ReplacementModel(reasoning="low"),
        ),
    )
    resolved = effective_feature(feature, config, tmp_path / "feature.md")
    replacement = resolved.models.resolve("replacement")
    assert replacement.model == "feature/coder"
    assert replacement.reasoning == "low"
    # Feature overrides only the fields it names; the worker keeps the
    # configured reasoning while its model follows the feature override.
    assert resolved.models.resolve("worker").reasoning == "low"


def test_replacement_follows_feature_worker_model_changes(tmp_path):
    global_file = tmp_path / "global.toml"
    global_file.write_text(
        '[models.worker]\nmodel = "global/coder"\nreasoning = "low"\n'
        '[models.replacement]\nreasoning = "high"\n'
    )
    config = load_config(None, global_file)
    for worker_model in ("feature/first", "feature/second"):
        feature = feature_for(
            tmp_path, RoleModels(worker=ModelSpec(model=worker_model))
        )
        resolved = effective_feature(feature, config, tmp_path / "feature.md")
        replacement = resolved.models.resolve("replacement")
        assert replacement.model == worker_model
        assert replacement.reasoning == "high"
        assert resolved.models.resolve("worker").reasoning == "low"


def test_feature_cannot_declare_an_independent_replacement_model(tmp_path):
    path = tmp_path / "feature.md"
    path.write_text(
        "---\n"
        "slug: demo\n"
        "target_project: .\n"
        "requirement: requirement.md\n"
        "models:\n"
        "  replacement:\n"
        "    model: rogue/model\n"
        "---\n"
    )
    with pytest.raises(ContractError, match="feature.md"):
        read_markdown(path, FeatureSpec)
    with pytest.raises(ValidationError):
        RoleModels.model_validate({"replacement": {"model": "rogue/model"}})


def test_changing_replacement_reasoning_invalidates_approval(plan_factory, tmp_path):
    plan, manifest = plan_factory()
    feature = feature_for(
        tmp_path,
        RoleModels(
            worker=ModelSpec(model="provider/coder", reasoning="low"),
            replacement=ReplacementModel(reasoning="low"),
        ),
    )
    low = effective_feature(feature, Config(), plan / "feature.md")
    approved = approval_hashes(plan, manifest, low)

    raised_feature = feature_for(
        tmp_path,
        RoleModels(
            worker=ModelSpec(model="provider/coder", reasoning="low"),
            replacement=ReplacementModel(reasoning="high"),
        ),
    )
    raised = effective_feature(raised_feature, Config(), plan / "feature.md")
    assert approval_hashes(plan, manifest, raised) != approved
    assert detect_approval_changes(approved, plan, manifest, raised) == [
        "effective_configuration: content changed"
    ]


def test_changing_configured_replacement_reasoning_invalidates_approval(
    plan_factory, tmp_path
):
    plan, manifest = plan_factory()
    feature = feature_for(
        tmp_path, RoleModels(worker=ModelSpec(model="provider/coder", reasoning="low"))
    )
    low = effective_feature(
        feature,
        Config(
            models=RoleModels(replacement=ReplacementModel(reasoning="low"))
        ),
        plan / "feature.md",
    )
    approved = approval_hashes(plan, manifest, low)
    high = effective_feature(
        feature,
        Config(
            models=RoleModels(replacement=ReplacementModel(reasoning="high"))
        ),
        plan / "feature.md",
    )
    assert approval_hashes(plan, manifest, high) != approved
    assert detect_approval_changes(approved, plan, manifest, high) == [
        "effective_configuration: content changed"
    ]


def test_adapters_receive_resolved_worker_and_replacement_reasoning(tmp_path):
    models = RoleModels(
        worker=ModelSpec(model="provider/coder", reasoning="low"),
        replacement=ReplacementModel(reasoning="high"),
    )
    adapter = OpenCodeAdapter()
    adapter._capabilities = AgentCapabilities(
        "opencode v2.1.4",
        frozenset({"--format", "--model", "--standalone", "--auto"}),
    )

    def request(role: str) -> AgentRequest:
        return AgentRequest(
            role=role,
            logical_id="w1",
            attempt_id="a1",
            instruction_file=tmp_path / "instruction.md",
            output_file=tmp_path / "handover.md",
            cwd=tmp_path,
            model=models.resolve(role),
            log_dir=tmp_path / "logs",
        )

    worker_argv = adapter.command(request("worker"))
    replacement_argv = adapter.command(request("replacement"))
    assert "provider/coder#low" in worker_argv
    assert "provider/coder#high" in replacement_argv


def test_cli_repository_replacement_update_never_copies_the_worker_model(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from orch.config import global_config_path, resolve_config
    from orch.models import inspect_preferences, update_preferences

    global_file = global_config_path()
    global_file.parent.mkdir(parents=True)
    global_file.write_text(
        '[models.worker]\nmodel = "provider/coder"\nreasoning = "low"\n'
        '[models.replacement]\ninherit = "worker"\nreasoning = "high"\n')
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / ".orch.toml"
    update_preferences(path=target, role="replacement", reasoning="medium", repo=repo)
    text = target.read_text()
    assert 'inherit = "worker"' in text
    assert 'reasoning = "medium"' in text
    assert "provider/coder" not in text
    assert "model =" not in text
    view = inspect_preferences(resolve_config(repo))
    replacement = next(row for row in view.rows if row.role == "replacement")
    assert replacement.model == "provider/coder"
    assert replacement.reasoning == "medium"

    update_preferences(path=target, role="replacement", reasoning="inherit", repo=repo)
    replacement = next(
        row for row in inspect_preferences(resolve_config(repo)).rows
        if row.role == "replacement")
    assert replacement.reasoning == "high"

