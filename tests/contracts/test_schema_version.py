"""Explicit state schema version, version-one retention and migration rules."""
import json

import pytest

from orch.contracts import (
    ActivityRecord,
    AssignmentState,
    ContractError,
    DependencyRecord,
    IssueState,
    RunState,
    migrate_v1_state,
    require_operational_schema,
)


def legacy_state():
    ambiguous = IssueState(
        id="i1", worker="w1", root_issue_id="i1", status="open",
        owner_role="code", evidence=["round-1/verdict-code.md"],
        assignments=[AssignmentState(repairs=1, failed_repairs=1, review_rounds=1,
                                     assessed_repairs=1, failed_repair_ordinals=[1])])
    resolved = IssueState(id="i2", worker="w1", root_issue_id="i2", status="resolved",
                          owner_role="code",
                          assignments=[AssignmentState(repairs=0, assessed_repairs=0,
                                                       failed_repair_ordinals=[])])
    return RunState(run_id="r1", slug="demo", version=1,
                    issues={"i1": ambiguous, "i2": resolved})


def test_newly_created_runs_use_the_canonical_schema(orch_store):
    state = RunState(run_id="r1", slug="demo")
    assert state.version == 2
    state.activity.append(ActivityRecord(activity_id="a1", kind="review", issue_id="i1"))
    assert "review_applications" in state.model_dump(mode="json")
    saved = orch_store.save(state)
    loaded = orch_store.load("r1")
    assert loaded.version == 2 and loaded.revision == saved.revision
    raw = json.loads((orch_store.run_dir("r1") / "state.json").read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert raw["activity"][0]["activity_id"] == "a1"


def test_version_one_state_loads_and_renders_without_rewrite(orch_store):
    state = legacy_state()
    orch_store.save(state)
    path = orch_store.run_dir("r1") / "state.json"
    before = path.read_bytes()
    loaded = orch_store.load("r1")
    assert loaded.version == 1
    assert loaded.issues["i1"].status == "open"
    assert loaded.issues["i1"].lifecycle == "ambiguous"
    assert path.read_bytes() == before
    rendered = (orch_store.run_dir("r1") / "issues.md").read_text(encoding="utf-8")
    assert "status=open" in rendered
    assert "lifecycle=ambiguous" in rendered
    assert "lifecycle=finished" in rendered


def test_operational_use_of_version_one_requires_explicit_migration(orch_store):
    state = legacy_state()
    with pytest.raises(ContractError, match="explicit compatible migration"):
        require_operational_schema(state)
    with pytest.raises(ContractError, match="already uses schema"):
        migrate_v1_state(RunState(run_id="r2", slug="demo"))


def test_migration_requires_a_complete_explicit_inventory(orch_store):
    state = legacy_state()
    with pytest.raises(ContractError, match="explicit pending/in_repair mapping"):
        migrate_v1_state(state)
    with pytest.raises(ContractError, match="incomplete"):
        migrate_v1_state(state, issue_lifecycles={"i1": "in_repair"},
                         assignment_ids={"i1": []})
    with pytest.raises(ContractError, match="unknown issues"):
        migrate_v1_state(state, issue_lifecycles={"i1": "in_repair", "i9": "pending"})
    with pytest.raises(ContractError, match="unknown issue"):
        migrate_v1_state(state, issue_lifecycles={"i1": "in_repair"},
                         assignment_ids={"i9": ["a"]})


def test_explicit_migration_preserves_counters_evidence_and_lineage(orch_store):
    original = legacy_state()
    migrated = migrate_v1_state(
        original, issue_lifecycles={"i1": "in_repair"},
        assignment_ids={"i1": ["i1:0"], "i2": ["i2:0"]})
    assert migrated.version == 2
    assert migrated.issues["i1"].status == "in_repair"
    assert migrated.issues["i1"].legacy_status == "open"
    assert migrated.issues["i1"].evidence == ["round-1/verdict-code.md"]
    assignment = migrated.issues["i1"].assignments[0]
    assert (assignment.repairs, assignment.failed_repairs, assignment.review_rounds,
            assignment.assessed_repairs) == (1, 1, 1, 1)
    assert assignment.assignment_id == "i1:0"
    assert migrated.issues["i2"].status == "finished"
    require_operational_schema(migrated)
    orch_store.save(migrated)
    raw = json.loads((orch_store.run_dir("r1") / "state.json").read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert raw["issues"]["i1"]["legacy_status"] == "open"


def test_unsupported_versions_still_require_migration(orch_store):
    orch_store.save(RunState(run_id="r1", slug="demo"))
    path = orch_store.run_dir("r1") / "state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for version in (0, 3, "2"):
        data["version"] = version
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ContractError, match="unsupported run state version"):
            orch_store.load("r1")


def test_dependency_record_roundtrips_with_state(orch_store):
    state = RunState(run_id="r1", slug="demo",
                     dependency_record=DependencyRecord(
                         plan_version="plan-1", source_verdicts=["review-result:t1"]))
    orch_store.save(state)
    assert orch_store.load("r1").dependency_record.plan_version == "plan-1"
