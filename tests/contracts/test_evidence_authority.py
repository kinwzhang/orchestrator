"""Evidence classification and the single authoritative entry point."""
import pytest

from orch.contracts import (
    AssignmentState,
    EvidenceConflictError,
    Finding,
    IssueState,
    ReviewResult,
    RunState,
    UnverifiableEvidenceError,
    Verdict,
    _digest,
    build_review_application,
    classify_review_evidence,
    require_authoritative_evidence,
)


def accepted_review(**updates):
    values = {
        "role": "code", "candidate_sha": "a" * 40, "evidence_digest": "b" * 64,
        "attempt_id": "attempt-1", "verdict_sha256": "c" * 64,
        "verdict_path": "round-1/verdict-code.md", "result": "rework",
        "findings": ["i1"], "resolved": [], "affected_workers": ["w1"],
        "accepted_at": "now", "transaction": "review-result:t1",
    }
    values.update(updates)
    return ReviewResult(**values)


def verified_state(complete=True):
    issue = IssueState(
        id="i1", worker="w1", root_issue_id="i1", status="pending", owner_role="code",
        assignments=[AssignmentState(assignment_id="i1:0", failed_repair_ordinals=[])])
    review = accepted_review()
    verdict = Verdict(reviewer="code", result="rework", candidate_sha="a" * 40,
                      findings=[Finding(id="i1", worker="w1", severity="major",
                                        action="rework_same_worker", summary="s",
                                        root_issue_id="i1")])
    application = build_review_application(review, verdict, {"i1": issue})
    decision = {"kind": "finding", "issue": "i1"}
    application.entries[0] = application.entries[0].model_copy(
        update={"state": "applied", "decision": decision, "applied_at": "now"})
    if complete:
        application.complete = True
        application.completed_at = "now"
    events = [
        {"kind": "review_entry_applied", "application_id": application.application_id,
         "entry_id": "finding:i1", "decision_digest": _digest(decision)},
        {"kind": "review_result_accepted", "transaction": review.transaction},
    ]
    state = RunState(run_id="r1", slug="demo", issues={"i1": issue},
                     review_results={review.transaction: review},
                     review_applications={application.application_id: application})
    return state, application, events


def test_verified_evidence_is_authoritative_with_complete_provenance():
    state, application, events = verified_state()
    assessment = classify_review_evidence(
        state, application.application_id, operation="acceptance", events=events)
    assert assessment.classification == "verified"
    assert assessment.authoritative is True
    assert assessment.provenance.reviewer_role == "code"
    require_authoritative_evidence(
        state, application.application_id, operation="acceptance", events=events)


def test_recomputed_unbound_is_diagnostic_only():
    state, application, events = verified_state()
    state.review_results = {}
    assessment = classify_review_evidence(
        state, application.application_id, operation="reconstruction", events=events)
    assert assessment.classification == "recomputed_unbound"
    assert assessment.authoritative is False
    assert "original candidate binding" in assessment.missing_inputs
    with pytest.raises(UnverifiableEvidenceError, match="recomputed"):
        require_authoritative_evidence(
            state, application.application_id, operation="reconstruction", events=events)


def test_missing_inputs_and_missing_digests_are_distinct_facts():
    state, application, _ = verified_state()
    absent = classify_review_evidence(state, "review-application:missing",
                                      operation="acceptance", events=[])
    assert absent.classification == "unverifiable"
    assert absent.missing_inputs == ["review application"]
    assert absent.missing_digests == []

    broken = application.model_copy(update={"evidence_digest": ""})
    state = state.model_copy(update={
        "review_applications": {broken.application_id: broken}})
    digest = classify_review_evidence(
        state, broken.application_id, operation="gap_check", events=[])
    assert digest.classification == "unverifiable"
    assert digest.missing_digests == ["approved-input digest"]
    assert digest.missing_inputs == []


def test_candidate_binding_conflict_stops_authoritative_use():
    state, application, events = verified_state()
    state.review_results[application.transaction] = accepted_review(
        candidate_sha="d" * 40)
    assessment = classify_review_evidence(
        state, application.application_id, operation="acceptance", events=events)
    assert assessment.classification == "conflict"
    assert any("candidate" in item for item in assessment.conflicts)
    with pytest.raises(EvidenceConflictError, match="conflicting evidence"):
        require_authoritative_evidence(
            state, application.application_id, operation="acceptance", events=events)


def test_reviewer_ownership_conflict_is_refused():
    state, application, events = verified_state()
    state.issues["i1"].owner_role = "docs"
    assessment = classify_review_evidence(
        state, application.application_id, operation="acceptance", events=events)
    assert assessment.classification == "conflict"
    assert any("owned by docs" in item for item in assessment.conflicts)


def test_audit_decision_conflict_is_detected():
    state, application, events = verified_state()
    events = [event for event in events if event["kind"] != "review_entry_applied"]
    events.append({"kind": "review_entry_applied",
                   "application_id": application.application_id,
                   "entry_id": "finding:i1", "decision_digest": "0" * 64})
    assessment = classify_review_evidence(
        state, application.application_id, operation="acceptance", events=events)
    assert assessment.classification == "conflict"
    assert any("audit decision disagrees" in item for item in assessment.conflicts)


def test_unvalidated_issue_ownership_is_unverifiable():
    state, application, events = verified_state()
    state.issues["i1"].owner_role = None
    assessment = classify_review_evidence(
        state, application.application_id, operation="acceptance", events=events)
    assert assessment.classification == "unverifiable"
    assert "validated issue ownership" in assessment.missing_inputs


def test_unknown_operation_is_refused():
    state, application, _ = verified_state()
    with pytest.raises(Exception, match="Unknown evidence consumer"):
        classify_review_evidence(state, application.application_id, operation="guess")
