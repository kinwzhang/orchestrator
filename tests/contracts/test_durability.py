"""Durable state, event journal and physical Markdown evidence."""
import builtins
import json

import pytest

from orch.contracts import (
    ERROR_CATEGORIES,
    ApprovalRequiredError,
    CheckError,
    ConflictError,
    ContractError,
    InfrastructureError,
    InterruptedError,
    IssueState,
    RunState,
    error_category,
)
from orch.store import atomic_write


def test_corrupt_state_names_the_file(orch_store):
    orch_store.save(RunState(run_id="r1", slug="demo"))
    path = orch_store.run_dir("r1") / "state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ContractError, match="state.json"):
        orch_store.load("r1")


def test_unsupported_state_version_requires_migration(orch_store):
    saved = orch_store.save(RunState(run_id="r1", slug="demo"))
    path = orch_store.run_dir("r1") / "state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = 3
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ContractError, match="migration"):
        orch_store.load("r1")
    assert saved.version == 2


def test_state_without_schema_version_requires_explicit_migration(orch_store):
    orch_store.save(RunState(run_id="r1", slug="demo"))
    path = orch_store.run_dir("r1") / "state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("version")
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ContractError, match="no schema version"):
        orch_store.load("r1")


def test_corrupt_event_journal_names_line(orch_store):
    orch_store.event("r1", "dispatch_intent", agent="w1")
    path = orch_store.run_dir("r1") / "events.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"id": 2, broken\n')
    with pytest.raises(ContractError, match="events.jsonl:2"):
        orch_store.events("r1")


def test_operation_journal_merges_intent_then_result(orch_store):
    state = orch_store.save(RunState(run_id="r1", slug="demo"))
    state = orch_store.record_operation(state, "worktree-create", {"intent": "create worker worktree"})
    first_seen = state.operations["worktree-create"]["first_seen"]
    state = orch_store.record_operation(state, "worktree-create",
                                        {"result": "ready", "sha": "abc123", "kind": "ignored"})
    stored = orch_store.load("r1").operations["worktree-create"]
    assert stored["intent"] == "create worker worktree"
    assert stored["result"] == "ready"
    assert stored["sha"] == "abc123"
    assert stored["first_seen"] == first_seen
    assert "kind" not in stored
    kinds = [event["kind"] for event in orch_store.events("r1")]
    assert kinds == ["operation", "operation"]
    assert [event["id"] for event in orch_store.events("r1")] == [1, 2]


def test_markdown_evidence_lists_agents_checks_and_issue_lineage(orch_store):
    state = orch_store.save(RunState(run_id="r1", slug="demo"))
    state.workers["w1"] = {"status": "done", "role": "worker", "attempt": 2,
                           "branch": "orch/demo/r1/w1", "worktree": "/tmp/w1",
                           "handover": "/tmp/handover.md", "checks": "pass",
                           "infrastructure_retries": 1}
    state.issues["f1"] = IssueState(id="f1", worker="w1", root_issue_id="f1",
                                    summary="Initial defect")
    state.issues["f1-1"] = IssueState(id="f1-1", worker="w1", root_issue_id="f1",
                                      parent_issue_id="f1", depth=1,
                                      evidence=["round-1/verdict-code.md"])
    state.issues["f1"].assignments[-1].repairs = 1
    state = orch_store.save(state)
    run = orch_store.run_dir("r1")
    checklist = (run / "checklist.md").read_text(encoding="utf-8")
    assert "- [x] w1:" in checklist
    assert "role=worker" in checklist and "attempt=2" in checklist
    assert "branch=orch/demo/r1/w1" in checklist
    assert "handover=/tmp/handover.md" in checklist and "retries=1" in checklist
    assert "Next action: PLAN" in checklist
    issues = (run / "issues.md").read_text(encoding="utf-8")
    assert "status=open; depth=0; owner=w1" in issues
    assert "parent=f1; attempt=1; reassignments=0" in issues
    assert "assignment 1: repairs=1; failed=0; review_rounds=0" in issues
    assert "evidence: round-1/verdict-code.md" in issues
    log = (run / "activity_log.md").read_text(encoding="utf-8")
    assert "| ID | UTC time | Event | Details |" in log
    assert state.updated_at is not None


def test_markdown_evidence_renders_without_event_or_state_duplication(orch_store):
    orch_store.save(RunState(run_id="r1", slug="demo"))
    orch_store.event("r1", "dispatch_intent", agent="w1")
    orch_store.render(orch_store.load("r1"))
    orch_store.render(orch_store.load("r1"))
    assert [event["id"] for event in orch_store.events("r1")] == [1]
    # A crash that loses only the Markdown is recovered from the journal.
    (orch_store.run_dir("r1") / "activity_log.md").unlink()
    orch_store.render_log("r1")
    assert "dispatch_intent" in (orch_store.run_dir("r1") / "activity_log.md").read_text(encoding="utf-8")


def test_checklist_next_action_can_be_supplied_by_the_workflow(orch_store):
    state = orch_store.save(RunState(run_id="r1", slug="demo", phase="RUNNING", stage="REVIEW"))
    orch_store.render(state, next_action="REVIEW")
    checklist = (orch_store.run_dir("r1") / "checklist.md").read_text(encoding="utf-8")
    assert "Next action: REVIEW" in checklist
    orch_store.render(orch_store.load("r1"))
    checklist = (orch_store.run_dir("r1") / "checklist.md").read_text(encoding="utf-8")
    assert "Next action: REVIEW" in checklist  # RUNNING renders its stage


def test_atomic_write_replaces_content_and_leaves_no_temporaries(orch_store):
    path = orch_store.data_dir / "nested" / "snapshot.md"
    atomic_write(path, "first\n")
    atomic_write(path, "second\n")
    assert path.read_text(encoding="utf-8") == "second\n"
    leftovers = [p for p in path.parent.iterdir() if p.name != "snapshot.md"]
    assert leftovers == []


def test_feature_lock_excludes_a_second_holder(orch_store):
    import portalocker

    from orch.store import StateStore

    other = StateStore(orch_store.data_dir)
    with (orch_store.lock("feature-demo"),
          pytest.raises(portalocker.exceptions.LockException),
          other.lock("feature-demo")):
        pass


def test_unsafe_run_ids_and_lock_names_are_rejected(orch_store):
    for bad in ("..", "a/b", "", ".hidden"):
        with pytest.raises(ValueError, match="Unsafe run ID"):
            orch_store.run_dir(bad)
    with pytest.raises(ValueError, match="Unsafe lock name"), orch_store.lock("../escape"):
        pass


def test_domain_error_categories_are_stable():
    assert set(ERROR_CATEGORIES) == {
        "validation", "approval_required", "infrastructure", "check", "conflict", "interrupted"}
    assert error_category(ContractError("bad input")) == "validation"
    assert error_category(ApprovalRequiredError("approve")) == "approval_required"
    assert error_category(InfrastructureError("missing tool")) == "infrastructure"
    assert error_category(CheckError("tests failed")) == "check"
    assert error_category(ConflictError("merge conflict")) == "conflict"
    assert error_category(InterruptedError("paused")) == "interrupted"
    assert error_category(ValueError("plain")) == "validation"
    assert error_category(TimeoutError("slow")) == "interrupted"
    assert error_category(builtins.InterruptedError("stopped")) == "interrupted"
    assert error_category(OSError("gone")) == "infrastructure"


def test_foreign_error_classes_map_to_categories():
    from orch.git_ops import ConflictRecord, GitConflict
    from orch.workflow import ApprovalRequired, Escalation

    record = ConflictRecord(path="/w", operation="merge", target_head="abc",
                            source_commits=["def"], paths=["a.py"])
    assert error_category(GitConflict(record)) == "conflict"
    assert error_category(ApprovalRequired("inputs changed")) == "approval_required"
    assert error_category(Escalation("budget exhausted")) == "infrastructure"
