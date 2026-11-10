"""Once-per-repair accounting with mandatory reviewer provenance."""
import pytest

from orch.contracts import (
    AssignmentState,
    ContractError,
    EvidenceConflictError,
    IssueState,
    ReviewerProvenance,
    UnverifiableEvidenceError,
    assess_repair_outcome,
)


def provenance(transaction="review-result:t1", role="code", **updates):
    values = {
        "reviewer_role": role,
        "attempt_id": "attempt-1",
        "candidate_sha": "a" * 40,
        "verdict_sha256": "b" * 64,
        "evidence_digest": "c" * 64,
        "verdict_path": "round-1/verdict-code.md",
        "transaction": transaction,
        "ownership_valid": True,
        "dispatch_evidence": True,
        "completion_evidence": True,
    }
    values.update(updates)
    return ReviewerProvenance(**values)


def issue_with(repairs=0, failed=0, assessed=None, ordinals=None, status="pending",
               rounds=0):
    return IssueState(
        id="i1", worker="w1", root_issue_id="i1", status=status,
        assignments=[AssignmentState(
            assignment_id="i1:0", repairs=repairs, failed_repairs=failed,
            review_rounds=rounds, assessed_repairs=assessed,
            failed_repair_ordinals=(ordinals if ordinals is not None else []))],
    )


def test_missing_provenance_refuses_authoritative_accounting():
    issue = issue_with(repairs=1)
    before = issue.model_dump(mode="json")
    with pytest.raises(UnverifiableEvidenceError, match="no weaker caller mode"):
        assess_repair_outcome(issue, "i1", None)
    assert issue.model_dump(mode="json") == before
    incomplete = provenance(ownership_valid=False)
    with pytest.raises(UnverifiableEvidenceError, match="ownership"):
        assess_repair_outcome(issue, "i1", incomplete)
    missing_dispatch = provenance(dispatch_evidence=False)
    with pytest.raises(UnverifiableEvidenceError, match="dispatch and completion"):
        assess_repair_outcome(issue, "i1", missing_dispatch)


def test_ordinal_zero_initial_work_never_charges_an_allowance():
    issue = issue_with(repairs=0)
    charge = assess_repair_outcome(issue, "i1", provenance())
    assert charge.assessment == "initial_work"
    assert charge.failed_repair_charged is False
    assert charge.review_round_charged is False
    assignment = issue.assignments[-1]
    assert (assignment.failed_repairs, assignment.review_rounds) == (0, 0)
    assert assignment.assessed_repairs == 0
    assert assignment.outcomes[-1].outcome == "initial_work"


def test_new_failure_charges_exactly_once_across_replay():
    issue = issue_with(repairs=1)
    first = assess_repair_outcome(issue, "i1", provenance())
    assert first.assessment == "new_failure"
    assert first.failed_repair_charged and first.review_round_charged
    assignment = issue.assignments[-1]
    assert (assignment.failed_repairs, assignment.review_rounds) == (1, 1)
    assert assignment.failed_repair_ordinals == [1]

    # A duplicate review of the same outcome creates no worker outcome.
    replay = assess_repair_outcome(issue, "i1", provenance(transaction="review-result:t2"))
    assert replay.assessment == "already_charged"
    assert replay.failed_repair_charged is False
    assert (assignment.failed_repairs, assignment.review_rounds) == (1, 1)
    assert len(assignment.outcomes) == 1

    # Approval-only revalidation of the same retained verdict is also free.
    revalidation = assess_repair_outcome(
        issue, "i1", provenance(transaction="review-result:t1"))
    assert revalidation.assessment == "already_charged"
    assert revalidation.failed_repair_charged is False


def test_repeated_findings_on_same_outcome_do_not_recharge():
    issue = issue_with(repairs=2, failed=1, assessed=2, ordinals=[1], rounds=1)
    charge = assess_repair_outcome(issue, "i1", provenance())
    assert charge.assessment == "already_assessed"
    assert charge.failed_repair_charged is False
    assert issue.assignments[-1].failed_repairs == 1


def test_resolution_marks_assessed_outcome_once_without_failure_charge():
    issue = issue_with(repairs=1, status="in_repair")
    charge = assess_repair_outcome(issue, "i1", provenance(), resolved=True)
    assert charge.assessment == "resolved"
    assert charge.failed_repair_charged is False
    assert charge.review_round_charged is True
    assignment = issue.assignments[-1]
    assert assignment.assessed_repairs == 1 and assignment.review_rounds == 1
    repeat = assess_repair_outcome(issue, "i1", provenance(transaction="review-result:t2"),
                                   resolved=True)
    assert repeat.assessment == "resolved"
    assert repeat.review_round_charged is False


def test_superseding_failure_charges_the_defective_repair_once():
    issue = issue_with(repairs=2, failed=1, assessed=2, ordinals=[1], rounds=1,
                       status="finished")
    charge = assess_repair_outcome(issue, "i1", provenance())
    assert charge.assessment == "superseding_failure"
    assert charge.failed_repair_charged is True
    assert charge.review_round_charged is False
    assignment = issue.assignments[-1]
    assert assignment.failed_repairs == 2
    assert assignment.failed_repair_ordinals == [1, 2]
    assert len(assignment.outcomes) == 1


def test_historical_assignment_attribution_and_ambiguity_refusal():
    older = AssignmentState(assignment_id="i1:0", repairs=1, assessed_repairs=1,
                            failed_repair_ordinals=[])
    newer = AssignmentState(assignment_id="i1:1", repairs=1, assessed_repairs=1,
                            failed_repair_ordinals=[])
    issue = IssueState(id="i1", worker="w1", root_issue_id="i1", status="finished",
                       assignments=[older, newer])
    with pytest.raises(ContractError, match="ambiguous"):
        assess_repair_outcome(issue, "i1", provenance())
    charge = assess_repair_outcome(issue, "i1", provenance(), assignment_index=0)
    assert charge.assignment_index == 0 and charge.assignment_id == "i1:0"
    assert older.failed_repairs == 1 and newer.failed_repairs == 0
    assert older.failed_repair_ordinals == [1]
    with pytest.raises(ContractError, match="unknown assignment index"):
        assess_repair_outcome(issue, "i1", provenance(), assignment_index=4)


def test_contradictory_outcome_history_is_refused():
    issue = issue_with(repairs=1, failed=1, assessed=1, ordinals=[1], rounds=1)
    assess_repair_outcome(issue, "i1", provenance())
    manual = issue.assignments[-1].outcomes[0].model_copy(update={"outcome": "successful"})
    issue.assignments[-1].outcomes[0] = manual
    with pytest.raises(EvidenceConflictError, match="contradicting"):
        assess_repair_outcome(issue, "i1", provenance())


def test_contradictory_counters_are_refused_before_mutation():
    issue = issue_with(repairs=1, failed=1, assessed=1, ordinals=[])
    before = issue.model_dump(mode="json")
    with pytest.raises(ContractError, match="ordinal provenance"):
        assess_repair_outcome(issue, "i1", provenance())
    assert issue.model_dump(mode="json") == before
