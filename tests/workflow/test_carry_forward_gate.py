"""Dispatch gate: carry-forward import must complete before any worker dispatch."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orch.carry_forward import CARRY_FORWARD_IMPORT_OPERATION, load_inventory
from orch.contracts import FeatureSpec, IssueState, RunState
from orch.git_ops import git
from orch.store import StateStore
from orch.workflow import (
    CARRY_FORWARD_IMPORT_PENDING,
    Engine,
    Escalation,
    carry_forward_gate_reason,
)


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


INVENTORY_TEMPLATE = """---
version: 1
predecessor_run_id: pred-1
predecessor_candidate_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
inventory_digest: "{digest}"
source_shas:
  s1: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
---

## Issue i1

- owner: w1
- owner_role: code
- root: i1
- parent: none
- depth: 0
- status: open
- lifecycle: pending
- attempt: 1
- reassignments: 0
- dispositions: none
- uncertainties: none
- evidence: none
- assignments:
  - i1:0; repairs=0; failed=0; rounds=0
- summary:
"""


def write_inventory(path):
    path.write_text(INVENTORY_TEMPLATE.format(digest="0" * 64), encoding="utf-8")
    unsigned = load_inventory(path, verify_digest=False)
    path.write_text(
        INVENTORY_TEMPLATE.format(digest=unsigned.computed_digest), encoding="utf-8")
    return load_inventory(path)


@pytest.fixture
def gate(tmp_path, plan_factory):
    plan, manifest = plan_factory()
    inventory = write_inventory(plan / "inventory.md")
    manifest = manifest.model_copy(
        update={"carry_forward_inventory": "inventory.md"})
    repo = make_repo(tmp_path)
    store = StateStore(tmp_path / "state")
    feature = FeatureSpec(
        slug="demo", target_project=repo, requirement=plan / "requirement.md")
    state = RunState(
        run_id="gate-run",
        slug="demo",
        phase="RUNNING",
        stage="WORK",
        workers={"w1": {"checked": False}},
        issues={
            "i1": IssueState(
                id="i1", worker="w1", root_issue_id="i1",
                owner_role="code", status="pending"),
        },
        workspace={
            "repo": str(repo),
            "feature_path": str(tmp_path / "checkout"),
            "feature_branch": "feature/demo",
            "base_sha": "0" * 40,
            "workers": {},
        },
    )
    state = store.save(state)
    engine = Engine(store, state, feature, manifest, plan, adapters={})
    return SimpleNamespace(
        tmp_path=tmp_path, plan=plan, manifest=manifest, feature=feature,
        store=store, state=state, engine=engine,
        digest=inventory.inventory_digest,
    )


def snapshot(state):
    return state.model_dump(mode="json")


def stub_dispatch(gate, dispatched):
    async def fake_run_worker(worker, active_resources=None):
        dispatched.append(worker.id)
        gate.engine.state.workers[worker.id]["checked"] = True
    gate.engine.run_worker = fake_run_worker


@pytest.mark.parametrize(
    ("operation", "detail"),
    [
        (None, "no durable import operation"),
        ({"status": "intent", "inventory_digest": "same"},
         "import status is 'intent'"),
        ({"status": "completed", "inventory_digest": "f" * 64},
         "does not match the approved inventory"),
        ({"status": "unrecognized", "inventory_digest": "same"},
         "import status is 'unrecognized'"),
    ],
)
def test_pending_import_refuses_dispatch_with_no_side_effects(gate, operation, detail):
    if operation is not None:
        if operation.get("inventory_digest") == "same":
            operation = {**operation, "inventory_digest": gate.digest}
        gate.engine.state.operations[CARRY_FORWARD_IMPORT_OPERATION] = operation
    before_state = snapshot(gate.engine.state)
    before_events = gate.store.events(gate.state.run_id)
    with pytest.raises(Escalation) as exc:
        asyncio.run(gate.engine.work())
    message = str(exc.value)
    assert CARRY_FORWARD_IMPORT_PENDING in message
    assert detail in message
    assert snapshot(gate.engine.state) == before_state
    assert gate.store.events(gate.state.run_id) == before_events
    assert gate.engine.state.issues["i1"].assignments[-1].repairs == 0
    assert gate.engine.state.workers["w1"].get("attempt", 0) == 0
    assert gate.engine.state.activity == []


def test_direct_run_worker_refuses_before_any_attempt(gate):
    before_state = snapshot(gate.engine.state)
    before_events = gate.store.events(gate.state.run_id)
    with pytest.raises(Escalation) as exc:
        asyncio.run(gate.engine.run_worker(gate.manifest.workers[0]))
    assert CARRY_FORWARD_IMPORT_PENDING in str(exc.value)
    assert snapshot(gate.engine.state) == before_state
    assert gate.store.events(gate.state.run_id) == before_events


def test_completed_matching_import_dispatches_normally(gate):
    gate.engine.state.operations[CARRY_FORWARD_IMPORT_OPERATION] = {
        "status": "completed", "inventory_digest": gate.digest}
    assert carry_forward_gate_reason(
        gate.engine.state, gate.plan, gate.manifest) is None
    dispatched: list[str] = []
    stub_dispatch(gate, dispatched)
    asyncio.run(gate.engine.work())
    assert dispatched == ["w1"]


def test_missing_manifest_inventory_leaves_gate_inert(gate):
    manifest = gate.manifest.model_copy(update={"carry_forward_inventory": None})
    engine = Engine(
        gate.store, gate.engine.state, gate.feature, manifest, gate.plan,
        adapters={})
    assert carry_forward_gate_reason(engine.state, engine.plan, manifest) is None
    dispatched: list[str] = []
    gate.engine = engine
    stub_dispatch(gate, dispatched)
    asyncio.run(engine.work())
    assert dispatched == ["w1"]
