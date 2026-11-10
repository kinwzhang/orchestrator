"""Durable per-entry review application and crash/replay boundaries."""
import pytest

from orch.contracts import (
    AssignmentState,
    EvidenceConflictError,
    Finding,
    IssueState,
    ReviewResult,
    RunState,
    Verdict,
    _digest,
    build_review_application,
)


def review_and_verdict(identities=("A",)):
    findings = [
        Finding(id=identity, worker="w1", severity="major",
                action="rework_same_worker", summary=f"fix {identity}",
                root_issue_id=identity)
        for identity in identities
    ]
    review = ReviewResult(
        role="code", candidate_sha="a" * 40, evidence_digest="b" * 64,
        attempt_id="attempt-1", verdict_sha256="c" * 64,
        verdict_path="round-1/verdict-code.md", result="rework",
        findings=list(identities), resolved=[], affected_workers=["w1"],
        accepted_at="now", transaction="review-result:t1",
    )
    verdict = Verdict(reviewer="code", result="rework", candidate_sha="a" * 40,
                      findings=findings)
    return review, verdict


def issue(identity="A", status="pending"):
    return IssueState(
        id=identity, worker="w1", root_issue_id=identity, status=status,
        owner_role="code",
        assignments=[AssignmentState(assignment_id=f"{identity}:0",
                                     failed_repair_ordinals=[])])


def prepared(orch_store, identities=("A",), statuses=None):
    statuses = statuses or {identity: "pending" for identity in identities}
    state = orch_store.save(RunState(run_id="r1", slug="demo"))
    state.issues = {identity: issue(identity, statuses[identity])
                    for identity in identities}
    state = orch_store.save(state)
    review, verdict = review_and_verdict(identities)
    application = build_review_application(review, verdict, state.issues)
    return state, review, application


def apply_entry(orch_store, state, application, identity, decision=None, status=None):
    entry = next(item for item in application.entries if item.issue_id == identity)
    updated = state.issues[identity].model_copy(deep=True)
    if status is not None:
        updated.status = status
    return orch_store.apply_review_entry(
        state, application.application_id, entry.entry_id,
        decision=decision or {"kind": entry.kind, "issue": identity}, issue=updated)


def test_build_is_deterministic_and_uses_stable_entry_identities(orch_store):
    state, _, application = prepared(orch_store, ("A", "B", "C"))
    assert [entry.entry_id for entry in application.entries] == [
        "finding:A", "finding:B", "finding:C"]
    assert [entry.order_index for entry in application.entries] == [0, 1, 2]
    assert application.next_entry_index == 0
    assert application.fully_applied is False
    rebuilt = build_review_application(
        review_and_verdict(("A", "B", "C"))[0], review_and_verdict(("A", "B", "C"))[1],
        state.issues)
    assert [entry.entry_id for entry in rebuilt.entries] == [
        entry.entry_id for entry in application.entries]


def test_repeated_intent_is_idempotent_and_conflicting_intent_is_refused(orch_store):
    state, _, application = prepared(orch_store)
    state = orch_store.begin_review_application(state, application)
    events = orch_store.events("r1")
    orch_store.begin_review_application(orch_store.load("r1"), application)
    assert orch_store.events("r1") == events
    changed = application.model_copy(update={"candidate_sha": "d" * 40})
    with pytest.raises(EvidenceConflictError, match="different durable intent"):
        orch_store.begin_review_application(orch_store.load("r1"), changed)


def test_crash_before_intent_leaves_nothing_applied(orch_store):
    state, _, application = prepared(orch_store)
    summary = orch_store.resume_review_application(state, application.application_id)
    assert summary == {"status": "absent", "application_id": application.application_id,
                       "next_entry_index": None, "complete": False}
    assert orch_store.events("r1") == []


def test_crash_after_intent_resumes_at_first_entry(orch_store):
    state, _, application = prepared(orch_store)
    orch_store.begin_review_application(state, application)
    resumed = orch_store.resume_review_application(state, application.application_id)
    assert resumed["status"] == "pending"
    assert resumed["next_entry_index"] == 0
    assert resumed["complete"] is False
    assert orch_store.load("r1").review_applications[
        application.application_id].applied_entry_ids == []


def test_crash_after_entry_save_reconciles_missing_audit_and_completes(orch_store):
    state, _, application = prepared(orch_store)
    state = orch_store.begin_review_application(state, application)
    state = apply_entry(orch_store, state, application, "A")
    # Process stops here: the decision is durable but its audit event is not.
    restarted = orch_store.load("r1")
    summary = orch_store.resume_review_application(restarted, application.application_id)
    assert summary["status"] == "completed" and summary["complete"] is True
    kinds = [event["kind"] for event in orch_store.events("r1")]
    assert kinds.count("review_entry_applied") == 1
    assert kinds.count("review_application_completed") == 1
    stored = orch_store.load("r1").review_applications[application.application_id]
    assert stored.entries[0].state == "applied"
    assert stored.complete is True and stored.completed_at is not None


def test_crash_after_audit_before_completion_completes_on_resume(orch_store):
    state, _, application = prepared(orch_store)
    state = orch_store.begin_review_application(state, application)
    state = apply_entry(orch_store, state, application, "A")
    # Audit already durable; completion not yet recorded.
    orch_store.record_review_entry_audit(state, application.application_id, "finding:A")
    restarted = orch_store.load("r1")
    summary = orch_store.resume_review_application(restarted, application.application_id)
    assert summary["complete"] is True
    # A second resume must not duplicate any event.
    orch_store.resume_review_application(orch_store.load("r1"),
                                         application.application_id)
    kinds = [event["kind"] for event in orch_store.events("r1")]
    assert kinds.count("review_entry_applied") == 1
    assert kinds.count("review_application_completed") == 1


def test_completion_is_refused_until_every_entry_is_applied(orch_store):
    state, _, application = prepared(orch_store, ("A", "B"))
    state = orch_store.begin_review_application(state, application)
    state = apply_entry(orch_store, state, application, "A")
    with pytest.raises(Exception, match="unapplied entries: finding:B"):
        orch_store.complete_review_application(state, application.application_id)


def test_untouched_entries_keep_prior_status_and_counters(orch_store):
    identities = ("A", "B", "C", "D", "E", "F")
    state, _, application = prepared(orch_store, identities)
    before = {identity: state.issues[identity].model_dump(mode="json")
              for identity in identities}
    state = orch_store.begin_review_application(state, application)
    state = apply_entry(orch_store, state, application, "A", status="escalated")
    resumed = orch_store.resume_review_application(state, application.application_id)
    # A's escalation is issue-scoped: the remaining entries are still pending.
    assert resumed["complete"] is False
    assert resumed["next_entry_index"] == 1
    stored = orch_store.load("r1")
    assert stored.issues["A"].status == "escalated"
    for identity in identities[1:]:
        assert stored.issues[identity].model_dump(mode="json") == before[identity]
        entry = next(item for item in stored.review_applications[
            application.application_id].entries if item.issue_id == identity)
        assert entry.state == "not_applied"


def test_no_change_entry_is_still_an_applied_entry(orch_store):
    state, _, application = prepared(orch_store)
    state = orch_store.begin_review_application(state, application)
    decision = {"kind": "finding", "issue": "A", "lifecycle": "pending->pending",
                "no_change": True}
    state = apply_entry(orch_store, state, application, "A", decision=decision)
    stored = orch_store.load("r1").review_applications[application.application_id]
    assert stored.entries[0].state == "applied"
    assert stored.entries[0].decision == decision


def test_audit_and_decision_contradictions_are_refused(orch_store):
    state, _, application = prepared(orch_store)
    state = orch_store.begin_review_application(state, application)
    state = apply_entry(orch_store, state, application, "A")
    orch_store.event("r1", "review_entry_applied",
                     application_id=application.application_id, entry_id="finding:A",
                     transaction=application.transaction,
                     decision={"kind": "finding", "issue": "A"},
                     decision_digest="0" * 64)
    with pytest.raises(EvidenceConflictError, match="disagrees"):
        orch_store.resume_review_application(
            orch_store.load("r1"), application.application_id)


def test_audit_without_applied_decision_is_a_conflict(orch_store):
    state, _, application = prepared(orch_store)
    state = orch_store.begin_review_application(state, application)
    orch_store.event("r1", "review_entry_applied",
                     application_id=application.application_id, entry_id="finding:A",
                     transaction=application.transaction,
                     decision={"kind": "finding", "issue": "A"},
                     decision_digest=_digest({"kind": "finding", "issue": "A"}))
    with pytest.raises(EvidenceConflictError, match="no applied decision"):
        orch_store.resume_review_application(state, application.application_id)
