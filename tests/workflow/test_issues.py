import copy

import pytest

from orch.contracts import (
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
