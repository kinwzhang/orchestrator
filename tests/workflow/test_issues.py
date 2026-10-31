import copy

import pytest

from orch.contracts import (
    AssignmentState,
    ContractError,
    Finding,
    IssueState,
    Manifest,
    Verdict,
)
from orch.workflow import plan_role_routing


def manifest():
    return Manifest(slug="feature", branch="feature/feature", workers=[
        {"id": "w1", "title": "work", "instruction": "workers/w1.md", "allowed_paths": ["app/**"]}])


def finding(identity="f1", parent=None, root="f1", action="rework_same_worker"):
    return Finding(id=identity, worker="w1", severity="major", action=action,
                   summary="Fix acceptance behavior", parent_issue_id=parent, root_issue_id=root)


def code_verdict(*findings, resolved=(), result="rework"):
    return Verdict(reviewer="code", result=result, findings=list(findings), resolved=list(resolved))


def docs_verdict(*findings, resolved=(), result="rework"):
    return Verdict(reviewer="docs", result=result, findings=list(findings), resolved=list(resolved))


def apply(issues, verdict, role="code", limits=None):
    limits = limits or manifest()
    plan = plan_role_routing(issues, verdict, limits, role)
    assert plan.escalation is None
    issues = dict(issues)
    for identity in plan.touched:
        issues[identity] = plan.issues[identity]
    return issues


def test_replacement_fresh_budgets_once():
    issues = {}
    issues = apply(issues, code_verdict(finding()))
    issue = issues["f1"]
    for _ in range(2):
        issue.assignments[-1].repairs += 1
        issues = apply(issues, code_verdict(finding()))
        issue = issues["f1"]
    assert len(issue.assignments) == 2
    assert issue.assignments[-1].repairs == 0
    assert issue.assignments[-1].review_rounds == 0
    issue.assignments[-1].repairs = 1
    issues = apply(issues, code_verdict(finding()))
    issues["f1"].assignments[-1].repairs += 1
    plan = plan_role_routing(issues, code_verdict(finding()), manifest(), "code")
    assert plan.escalation == "Issue f1 exhausted replacement allowance"
    assert plan.issues["f1"].status == "escalated"


def test_derived_depth_three_escalates_root():
    issues = {}
    parent = None
    for identity in ("f1", "f1-1", "f1-1-1"):
        issues = apply(issues, code_verdict(finding(identity, parent)))
        parent = identity
    plan = plan_role_routing(issues, code_verdict(finding("f1-1-1-1", parent)), manifest(), "code")
    assert plan.escalation == "Derived issue f1-1-1-1 exceeds depth limit; escalate root f1"
    assert plan.issues["f1"].status == "escalated"
    assert plan.issues["f1-1-1-1"].assignments[0].repairs == 0


def test_passing_role_must_resolve_its_own_known_issues():
    issues = {"f1": IssueState(id="f1", worker="w1", root_issue_id="f1", owner_role="code")}
    with pytest.raises(ValueError, match="explicitly resolve"):
        plan_role_routing(issues, Verdict(reviewer="code", result="pass"), manifest(), "code")
    plan = plan_role_routing(
        issues, Verdict(reviewer="code", result="pass", resolved=["f1"]), manifest(), "code")
    assert plan.issues["f1"].status == "resolved"


def test_docs_pass_leaves_open_code_findings_untouched():
    issues = {"f1": IssueState(id="f1", worker="w1", root_issue_id="f1", owner_role="code")}
    plan = plan_role_routing(issues, Verdict(reviewer="docs", result="pass"), manifest(), "docs")
    assert plan.issues["f1"].status == "open"
    assert plan.touched == []


def test_cross_role_resolution_is_rejected_before_any_change():
    issues = {
        "code-1": IssueState(id="code-1", worker="w1", root_issue_id="code-1", owner_role="code"),
        "docs-1": IssueState(id="docs-1", worker="w1", root_issue_id="docs-1", owner_role="docs"),
    }
    original = copy.deepcopy(issues)
    verdict = Verdict(reviewer="docs", result="pass", resolved=["code-1"])
    with pytest.raises(ContractError, match="may not resolve code-1"):
        plan_role_routing(issues, verdict, manifest(), "docs")
    assert issues == original


def test_cross_role_finding_is_rejected():
    issues = {"code-1": IssueState(id="code-1", worker="w1", root_issue_id="code-1",
                                   owner_role="code")}
    with pytest.raises(ContractError, match="may not report code-1"):
        plan_role_routing(issues, docs_verdict(finding("code-1")), manifest(), "docs")


def test_unowned_legacy_issue_is_refused_without_evidence():
    issues = {"legacy": IssueState(id="legacy", worker="w1", root_issue_id="legacy")}
    with pytest.raises(ContractError, match="no validated owner"):
        plan_role_routing(issues, code_verdict(finding("legacy")), manifest(), "code")
    with pytest.raises(ContractError, match="no validated owner"):
        plan_role_routing(issues, Verdict(reviewer="code", result="rework",
                                          findings=[finding("new")],
                                          resolved=["legacy"]), manifest(), "code")


def test_system_issue_cannot_be_resolved_by_a_reviewer():
    issues = {"check-w1": IssueState(id="check-w1", worker="w1", root_issue_id="check-w1",
                                     owner_role="system")}
    with pytest.raises(ContractError, match="may not resolve"):
        plan_role_routing(issues, Verdict(reviewer="code", result="pass", resolved=["check-w1"]),
                          manifest(), "code")


def test_unavailable_native_evidence_escalates_instead_of_rework():
    plan = plan_role_routing({}, code_verdict(finding(action="escalate")), manifest(), "code")
    assert plan.escalation == "Issue f1 requires escalation"
    assert plan.issues["f1"].status == "escalated"
    assert plan.issues["f1"].assignments[0].repairs == 0


def test_docs_new_finding_is_docs_owned_and_reopens_only_itself():
    issues = {}
    new = finding("docs-1", root="docs-1")
    plan = plan_role_routing(issues, docs_verdict(new), manifest(), "docs")
    assert plan.issues["docs-1"].owner_role == "docs"
    plan = plan_role_routing(plan.issues, docs_verdict(new), manifest(), "docs")
    issue = plan.issues["docs-1"]
    assert issue.assignments[-1].review_rounds == 1
    assert issue.assignments[-1].failed_repairs == 0


def live_shaped_issues(repairs=1, failed_repairs=1, review_rounds=1, assessed_repairs=None):
    return {
        identity: IssueState(
            id=identity, worker="w1", root_issue_id=identity, owner_role="code",
            status="open",
            assignments=[AssignmentState(
                repairs=repairs, failed_repairs=failed_repairs,
                review_rounds=review_rounds, assessed_repairs=assessed_repairs,
            )],
        )
        for identity in ("code-w01-02", "code-w01-03")
    }


def test_revalidating_an_already_charged_repair_keeps_counters_and_assignment():
    issues = live_shaped_issues()
    original = copy.deepcopy(issues)
    verdict = code_verdict(finding("code-w01-02", root="code-w01-02"), finding("code-w01-03", root="code-w01-03"))
    plan = plan_role_routing(issues, verdict, manifest(), "code")
    assert plan.escalation is None
    assert issues == original  # planning is still pure
    for identity in ("code-w01-02", "code-w01-03"):
        issue = plan.issues[identity]
        assert len(issue.assignments) == 1
        assignment = issue.assignments[0]
        assert assignment.repairs == 1
        assert assignment.failed_repairs == 1
        assert assignment.review_rounds == 1
        assert assignment.assessed_repairs == 1


def test_each_distinct_unsuccessful_repair_is_charged_once_then_replaces():
    issues = live_shaped_issues(failed_repairs=0, review_rounds=0)
    verdict = code_verdict(finding("code-w01-02", root="code-w01-02"), finding("code-w01-03", root="code-w01-03"))
    first = plan_role_routing(issues, verdict, manifest(), "code")
    for identity in ("code-w01-02", "code-w01-03"):
        assignment = first.issues[identity].assignments[-1]
        assert (assignment.failed_repairs, assignment.review_rounds,
                assignment.assessed_repairs) == (1, 1, 1)
        assert len(first.issues[identity].assignments) == 1

    # The same repair outcome routed again is not a second failure.
    repeated = plan_role_routing(first.issues, verdict, manifest(), "code")
    for identity in ("code-w01-02", "code-w01-03"):
        assignment = repeated.issues[identity].assignments[-1]
        assert (assignment.failed_repairs, assignment.review_rounds) == (1, 1)

    # A genuinely new worker repair is charged once and exhausts the two-failure
    # allowance, applying the existing one-reassignment policy.
    repaired = copy.deepcopy(repeated.issues)
    for issue in repaired.values():
        issue.assignments[-1].repairs += 1
    second = plan_role_routing(repaired, verdict, manifest(), "code")
    for identity in ("code-w01-02", "code-w01-03"):
        issue = second.issues[identity]
        assert len(issue.assignments) == 2
        assert issue.assignments[0].failed_repairs == 2
        assert issue.assignments[0].review_rounds == 2
        assert issue.assignments[0].assessed_repairs == 2
        assert issue.assignments[-1].failed_repairs == 0

    # Revalidating the feedback after the replacement must not charge a new
    # review round on the not-yet-repaired replacement or reassign again.
    after_replacement = plan_role_routing(second.issues, verdict, manifest(), "code")
    for identity in ("code-w01-02", "code-w01-03"):
        issue = after_replacement.issues[identity]
        assert len(issue.assignments) == 2
        assert issue.assignments[-1].failed_repairs == 0
        assert issue.assignments[-1].review_rounds == 0


def test_legacy_repair_counters_are_normalised_per_issue():
    issues = {
        "already-charged": IssueState(
            id="already-charged", worker="w1", root_issue_id="already-charged",
            owner_role="code", status="open",
            assignments=[AssignmentState(repairs=1, failed_repairs=1, review_rounds=1)]),
        "new-outcome": IssueState(
            id="new-outcome", worker="w1", root_issue_id="new-outcome",
            owner_role="code", status="open",
            assignments=[AssignmentState(repairs=2, failed_repairs=0, review_rounds=0)]),
    }
    plan = plan_role_routing(
        issues,
        code_verdict(finding("already-charged", root="already-charged"), finding("new-outcome", root="new-outcome")),
        manifest(), "code",
    )
    charged = plan.issues["already-charged"].assignments[-1]
    assert (charged.failed_repairs, charged.review_rounds, charged.assessed_repairs) == (1, 1, 1)
    new = plan.issues["new-outcome"].assignments[-1]
    assert (new.failed_repairs, new.review_rounds, new.assessed_repairs) == (1, 1, 2)
    assert len(plan.issues["new-outcome"].assignments) == 1


def test_contradictory_legacy_repair_evidence_refuses_without_mutation():
    issues = live_shaped_issues(repairs=1, failed_repairs=2, review_rounds=2)
    original = copy.deepcopy(issues)
    with pytest.raises(ContractError, match="ambiguous whether the latest repair"):
        plan_role_routing(issues, code_verdict(finding("code-w01-02", root="code-w01-02")), manifest(), "code")
    assert issues == original


def test_repair_revalidation_is_independent_between_reviewer_roles():
    issues = {
        "code-1": IssueState(
            id="code-1", worker="w1", root_issue_id="code-1", owner_role="code",
            status="open",
            assignments=[AssignmentState(repairs=1, failed_repairs=1, review_rounds=1)]),
        "docs-1": IssueState(
            id="docs-1", worker="w1", root_issue_id="docs-1", owner_role="docs",
            status="open",
            assignments=[AssignmentState(repairs=1, failed_repairs=1, review_rounds=1)]),
    }
    code_plan = plan_role_routing(issues, code_verdict(finding("code-1", root="code-1")), manifest(), "code")
    assert code_plan.touched == ["code-1"]
    assert code_plan.issues["code-1"].assignments[-1].failed_repairs == 1
    assert code_plan.issues["docs-1"].assignments[-1].failed_repairs == 1

    docs_plan = plan_role_routing(
        code_plan.issues, docs_verdict(finding("docs-1", root="docs-1")), manifest(), "docs")
    assert docs_plan.touched == ["docs-1"]
    assert docs_plan.issues["docs-1"].assignments[-1].failed_repairs == 1
    assert docs_plan.issues["code-1"].assignments[-1].failed_repairs == 1
