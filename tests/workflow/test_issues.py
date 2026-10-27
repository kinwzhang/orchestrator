import pytest

from orch.contracts import Finding, IssueState, Manifest, RunState, Verdict
from orch.workflow import Escalation, route_verdicts


def manifest():
    return Manifest(slug="feature", branch="feature/feature", workers=[
        {"id": "w1", "title": "work", "instruction": "workers/w1.md", "allowed_paths": ["app/**"]}])


def finding(identity="f1", parent=None, root="f1"):
    return Finding(id=identity, worker="w1", severity="major", action="rework_same_worker",
                   summary="Fix acceptance behavior", parent_issue_id=parent, root_issue_id=root)


def verdict(item):
    return [Verdict(reviewer="code", result="rework", findings=[item]),
            Verdict(reviewer="docs", result="pass")]


def test_replacement_fresh_budgets_once():
    state = RunState(run_id="run", slug="feature")
    route_verdicts(state, verdict(finding()), manifest())
    issue = state.issues["f1"]
    for _ in range(2):
        issue.assignments[-1].repairs += 1
        route_verdicts(state, verdict(finding()), manifest())
    assert len(issue.assignments) == 2
    assert issue.assignments[-1].repairs == 0
    assert issue.assignments[-1].review_rounds == 0
    issue.assignments[-1].repairs = 1
    route_verdicts(state, verdict(finding()), manifest())
    issue.assignments[-1].repairs += 1
    with pytest.raises(Escalation, match="replacement"):
        route_verdicts(state, verdict(finding()), manifest())


def test_derived_depth_three_escalates_root():
    state = RunState(run_id="run", slug="feature")
    parent = None
    for identity in ("f1", "f1-1", "f1-1-1"):
        route_verdicts(state, verdict(finding(identity, parent)), manifest())
        parent = identity
    with pytest.raises(Escalation, match="root f1"):
        route_verdicts(state, verdict(finding("f1-1-1-1", parent)), manifest())
    assert state.issues["f1"].status == "escalated"
    assert state.issues["f1-1-1-1"].assignments[0].repairs == 0


def test_passing_review_must_resolve_known_issues():
    state = RunState(run_id="run", slug="feature", issues={
        "f1": IssueState(id="f1", worker="w1", root_issue_id="f1")})
    with pytest.raises(ValueError, match="explicitly resolve"):
        route_verdicts(state, [Verdict(reviewer="code", result="pass")], manifest())
    route_verdicts(state, [Verdict(reviewer="code", result="pass", resolved=["f1"])], manifest())
    assert state.issues["f1"].status == "resolved"
