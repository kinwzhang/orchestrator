"""Engine-to-store human disposition audit and reconciliation coverage.

Every test uses a real ``Engine``, a temporary ``StateStore`` and a throwaway
git repository. No live run, agent, paid call or shared state is touched.
"""
from __future__ import annotations

import pytest

from orch.contracts import (
    AssignmentState,
    FeatureSpec,
    HumanDisposition,
    IssueState,
    LifecycleTransition,
    RunState,
    apply_human_disposition,
)
from orch.git_ops import git
from orch.store import StateStore
from orch.workflow import Engine, _accounting_digest

ISSUE_ID = "code-w01-03"


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


def working_issue(identity=ISSUE_ID, status="escalated"):
    return IssueState(
        id=identity, worker="w1", root_issue_id=identity, owner_role="code",
        status=status, attempt=9,
        assignments=[
            AssignmentState(
                assignment_id=f"{identity}:0", repairs=2, failed_repairs=2,
                review_rounds=2, assessed_repairs=2, failed_repair_ordinals=[1, 2]),
            AssignmentState(
                assignment_id=f"{identity}:1", repairs=2, failed_repairs=2,
                review_rounds=2, assessed_repairs=2, failed_repair_ordinals=[1, 2]),
        ],
    )


def acceptance(scope="scoped acceptance of the retained candidate",
               evidence=None):
    return HumanDisposition(
        kind="scoped_acceptance", issue_id=ISSUE_ID, scope=scope,
        evidence=list(evidence or ["verdict:accepted", "operator:authorized"]),
        allowance_disposition="exhausted allowance retained; no recharge",
    )


def continuation(scope="continue the scoped repair"):
    return HumanDisposition(
        kind="continuation", issue_id=ISSUE_ID, scope=scope,
        evidence=["operator:authorized"], allowance_disposition="continue existing",
        dependencies=["code-w01-01"],
    )


@pytest.fixture
def engine_factory(tmp_path, plan_factory):
    plan, manifest = plan_factory()
    repo = make_repo(tmp_path)

    def make(issues=None, run_id="hd-run"):
        store = StateStore(tmp_path / "state")
        state = RunState(
            run_id=run_id, slug="demo", phase="RUNNING", stage="WORK",
            workers={"w1": {"checked": False}},
            issues=issues or {ISSUE_ID: working_issue()},
            workspace={
                "repo": str(repo),
                "feature_path": str(tmp_path / "checkout"),
                "feature_branch": "feature/demo",
                "base_sha": "0" * 40,
                "workers": {},
            },
        )
        state = store.save(state)
        feature = FeatureSpec(
            slug="demo", target_project=repo, requirement=plan / "requirement.md")
        return Engine(store, state, feature, manifest, plan, adapters={})

    return make


def audits(store, run_id="hd-run"):
    return [event for event in store.events(run_id)
            if event.get("kind") == "human_disposition"]


def operation_for(engine, issue_id=ISSUE_ID):
    prefix = f"human-disposition:{issue_id}:"
    matches = [(key, value) for key, value in engine.state.operations.items()
               if key.startswith(prefix)]
    assert len(matches) == 1
    return matches[0]


def test_record_appends_one_named_audit_and_preserves_accounting(engine_factory):
    engine = engine_factory()
    before = engine.state.issues[ISSUE_ID].model_dump(mode="json")
    disposition = acceptance()

    engine.record_human_disposition(disposition)

    issue = engine.state.issues[ISSUE_ID]
    assert issue.status == "finished"
    assert issue.attempt == before["attempt"]
    assert [assignment.model_dump(mode="json") for assignment in issue.assignments] == (
        before["assignments"])
    retained = issue.lifecycle_history[-1]
    assert (retained.from_status, retained.to_status) == ("escalated", "finished")

    emitted = audits(engine.store)
    assert len(emitted) == 1
    event = emitted[0]
    # The detail key must not override the positional event kind.
    assert event["kind"] == "human_disposition"
    assert event["disposition_kind"] == "scoped_acceptance"
    assert event["issue"] == ISSUE_ID
    assert (event["from_status"], event["to_status"]) == ("escalated", "finished")
    assert event["scope"] == disposition.scope
    assert event["evidence"] == retained.evidence
    assert event["deferred_stage"] is None
    assert event["transition_recorded_at"] == retained.recorded_at
    assert event["operation"].startswith(f"human-disposition:{ISSUE_ID}:")

    _key, operation = operation_for(engine)
    assert operation["status"] == "completed"
    assert operation["audit_event_id"] == event["id"]
    assert operation["reconciled"] is False
    assert engine.recovery_audit_gaps() == []

    reloaded = engine.store.load("hd-run")
    assert reloaded.issues[ISSUE_ID].model_dump(mode="json") == (
        engine.state.issues[ISSUE_ID].model_dump(mode="json"))


def test_record_continuation_keeps_counters_and_names_continuation(engine_factory):
    engine = engine_factory()
    before = engine.state.issues[ISSUE_ID].model_dump(mode="json")

    engine.record_human_disposition(continuation())

    issue = engine.state.issues[ISSUE_ID]
    assert issue.status == "pending"
    assert issue.attempt == before["attempt"]
    assert [assignment.model_dump(mode="json") for assignment in issue.assignments] == (
        before["assignments"])
    event = audits(engine.store)[0]
    assert event["kind"] == "human_disposition"
    assert event["disposition_kind"] == "continuation"
    assert (event["from_status"], event["to_status"]) == ("escalated", "pending")
    assert "dependency:code-w01-01" in event["evidence"]


def test_record_refuses_unknown_issue(engine_factory):
    engine = engine_factory()
    events_before = len(engine.store.events("hd-run"))
    operations_before = dict(engine.state.operations)

    with pytest.raises(Exception, match="unknown issue"):
        engine.record_human_disposition(acceptance().model_copy(
            update={"issue_id": "missing"}))
    assert engine.state.issues[ISSUE_ID].status == "escalated"
    assert engine.state.issues[ISSUE_ID].lifecycle_history == []
    assert engine.state.operations == operations_before
    assert len(engine.store.events("hd-run")) == events_before


def test_record_refuses_illegal_transition(engine_factory):
    engine = engine_factory(issues={ISSUE_ID: working_issue(status="finished")})
    events_before = len(engine.store.events("hd-run"))

    with pytest.raises(Exception, match="same-status entry finished"):
        engine.record_human_disposition(acceptance())
    assert engine.state.issues[ISSUE_ID].status == "finished"
    assert engine.state.issues[ISSUE_ID].lifecycle_history == []
    assert engine.state.operations == {}
    assert len(engine.store.events("hd-run")) == events_before


def test_save_then_audit_crash_reconciles_exactly_once(engine_factory, monkeypatch):
    engine = engine_factory()
    disposition = acceptance()
    before = engine.state.issues[ISSUE_ID].model_dump(mode="json")
    real_event = engine.event

    def crash_before_audit(kind, **details):
        raise RuntimeError("simulated interruption before audit append")

    monkeypatch.setattr(engine, "event", crash_before_audit)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        engine.record_human_disposition(disposition)

    # The transition and intent are durable; the audit is absent.
    assert engine.state.issues[ISSUE_ID].status == "finished"
    assert audits(engine.store) == []
    key, operation = operation_for(engine)
    assert operation["status"] == "intent"
    gaps = engine.recovery_audit_gaps()
    assert len(gaps) == 1 and key in gaps[0]

    monkeypatch.setattr(engine, "event", real_event)
    event = engine.reconcile_human_disposition(disposition)

    assert event["reconciled"] is True
    assert event["transition_recorded_at"] == (
        engine.state.issues[ISSUE_ID].lifecycle_history[-1].recorded_at)
    assert event["reconciled_at"] and event["time"]
    assert len(audits(engine.store)) == 1
    issue = engine.state.issues[ISSUE_ID]
    assert issue.attempt == before["attempt"]
    assert [assignment.model_dump(mode="json") for assignment in issue.assignments] == (
        before["assignments"])
    _, completed = operation_for(engine)
    assert completed["status"] == "completed"
    assert completed["audit_event_id"] == event["id"]
    assert engine.recovery_audit_gaps() == []

    replay = engine.reconcile_human_disposition(disposition)
    assert replay["id"] == event["id"]
    assert len(audits(engine.store)) == 1


def test_interrupted_completion_is_idempotent(engine_factory, monkeypatch):
    engine = engine_factory()
    disposition = acceptance()
    real_record_operation = engine.store.record_operation

    def crash_on_completion(state, key, value):
        if value.get("status") == "completed":
            raise RuntimeError("simulated interruption before completion")
        return real_record_operation(state, key, value)

    monkeypatch.setattr(engine.store, "record_operation", crash_on_completion)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        engine.record_human_disposition(disposition)

    # The original audit is durable, but the operation still reads intent.
    assert len(audits(engine.store)) == 1
    key, operation = operation_for(engine)
    assert operation["status"] == "intent"
    assert key in " ".join(engine.recovery_audit_gaps())

    monkeypatch.setattr(engine.store, "record_operation", real_record_operation)
    event = engine.reconcile_human_disposition(disposition)

    assert event.get("reconciled") is not True
    assert len(audits(engine.store)) == 1
    _, completed = operation_for(engine)
    assert completed["status"] == "completed"
    assert completed["audit_event_id"] == event["id"]
    assert completed["reconciled"] is False
    assert engine.recovery_audit_gaps() == []

    replay = engine.reconcile_human_disposition(disposition)
    assert replay["id"] == event["id"]
    assert len(audits(engine.store)) == 1


def test_legacy_saved_transition_reconciles_without_operation(engine_factory):
    disposition = acceptance()
    issue = working_issue()
    apply_human_disposition(issue, disposition)
    retained = issue.lifecycle_history[-1]
    engine = engine_factory(issues={ISSUE_ID: issue})
    before = issue.model_dump(mode="json")

    event = engine.reconcile_human_disposition(disposition)

    assert event["reconciled"] is True
    assert "operation" not in event
    assert event["transition_recorded_at"] == retained.recorded_at
    assert event["reconciled_at"] and event["time"]
    assert len(audits(engine.store)) == 1
    issue_after = engine.state.issues[ISSUE_ID]
    assert issue_after.attempt == before["attempt"]
    assert [assignment.model_dump(mode="json") for assignment in issue_after.assignments] == (
        before["assignments"])
    assert engine.reconcile_human_disposition(disposition)["id"] == event["id"]
    assert len(audits(engine.store)) == 1


def test_finished_status_alone_is_not_eligibility(engine_factory):
    issue = working_issue(status="finished")
    engine = engine_factory(issues={ISSUE_ID: issue})

    with pytest.raises(Exception, match="no retained lifecycle transition"):
        engine.reconcile_human_disposition(acceptance())
    assert audits(engine.store) == []


def test_reconcile_refuses_mismatched_disposition(engine_factory):
    disposition = acceptance()
    issue = working_issue()
    apply_human_disposition(issue, disposition)
    engine = engine_factory(issues={ISSUE_ID: issue})

    with pytest.raises(Exception, match="does not match the retained transition"):
        engine.reconcile_human_disposition(acceptance(scope="different scope"))
    assert audits(engine.store) == []


def test_reconcile_refuses_ambiguous_transition_attribution(engine_factory):
    disposition = acceptance()
    issue = working_issue()
    apply_human_disposition(issue, disposition)
    duplicate = issue.lifecycle_history[-1].model_copy(deep=True)
    issue.lifecycle_history.append(duplicate)
    engine = engine_factory(issues={ISSUE_ID: issue})

    with pytest.raises(Exception, match="ambiguous"):
        engine.reconcile_human_disposition(disposition)
    assert audits(engine.store) == []


def test_reconcile_refuses_conflicting_and_duplicate_audits(engine_factory):
    disposition = acceptance()
    issue = working_issue()
    apply_human_disposition(issue, disposition)
    retained = issue.lifecycle_history[-1]
    engine = engine_factory(issues={ISSUE_ID: issue})

    engine.event("human_disposition", issue=ISSUE_ID,
                 disposition_kind="continuation",
                 from_status=retained.from_status,
                 to_status=retained.to_status,
                 scope=disposition.scope, evidence=retained.evidence,
                 deferred_stage=None,
                 transition_recorded_at=retained.recorded_at)
    with pytest.raises(Exception, match="disagrees on disposition_kind"):
        engine.reconcile_human_disposition(disposition)
    assert len(audits(engine.store)) == 1

    engine.event("human_disposition", issue=ISSUE_ID,
                 disposition_kind=disposition.kind,
                 from_status=retained.from_status,
                 to_status=retained.to_status,
                 scope=disposition.scope, evidence=retained.evidence,
                 deferred_stage=None,
                 transition_recorded_at=retained.recorded_at)
    with pytest.raises(Exception, match="duplicate reconciliation"):
        engine.reconcile_human_disposition(disposition)
    assert len(audits(engine.store)) == 2


def test_reconcile_refuses_audit_without_usable_identity(engine_factory):
    disposition = acceptance()
    issue = working_issue()
    apply_human_disposition(issue, disposition)
    retained = issue.lifecycle_history[-1]
    engine = engine_factory(issues={ISSUE_ID: issue})
    engine.event("human_disposition", issue=ISSUE_ID,
                 disposition_kind=disposition.kind,
                 from_status=retained.from_status,
                 to_status=retained.to_status,
                 scope=disposition.scope, evidence=retained.evidence,
                 deferred_stage=None)

    with pytest.raises(Exception, match="no usable transition identity"):
        engine.reconcile_human_disposition(disposition)
    assert len(audits(engine.store)) == 1


def test_reconcile_refuses_changed_counters_after_save(engine_factory):
    engine = engine_factory()
    disposition = acceptance()
    engine.record_human_disposition(disposition)
    issue = engine.state.issues[ISSUE_ID]
    issue.assignments[0].repairs += 1
    engine.save()

    with pytest.raises(Exception, match="counters or assignments changed"):
        engine.reconcile_human_disposition(disposition)
    assert len(audits(engine.store)) == 1


def test_unrelated_issue_and_audit_are_untouched(engine_factory):
    other = working_issue("code-w01-01", status="pending")
    engine = engine_factory(issues={ISSUE_ID: working_issue(), "code-w01-01": other})
    before_other = engine.state.issues["code-w01-01"].model_dump(mode="json")
    engine.event("human_disposition", issue="code-w01-01",
                 disposition_kind="continuation",
                 from_status="escalated", to_status="pending",
                 scope="unrelated", evidence=["unrelated"],
                 deferred_stage=None, transition_recorded_at="unrelated")

    engine.record_human_disposition(acceptance())

    assert engine.state.issues["code-w01-01"].model_dump(mode="json") == before_other
    assert [event["issue"] for event in audits(engine.store)] == [
        "code-w01-01", ISSUE_ID]


def test_accounting_digest_is_stable_and_excludes_lifecycle(engine_factory):
    engine = engine_factory()
    issue = engine.state.issues[ISSUE_ID]
    digest = _accounting_digest(issue)
    transition = LifecycleTransition(
        issue_id=ISSUE_ID, from_status="escalated", to_status="finished",
        guard="test", evidence=[])
    issue.lifecycle_history.append(transition)
    issue.status = "finished"
    assert _accounting_digest(issue) == digest
