"""Ordered per-entry application, resume and minimum dependency scheduling."""
import pytest

from orch.contracts import (
    AssignmentState,
    Finding,
    IssueState,
    Manifest,
    ReviewResult,
    RunState,
    Verdict,
    write_markdown,
)
from orch.reporting import render_report
from orch.store import StateStore
from orch.workflow import (
    accept_review_result,
    issue_dependency_blockers,
    plan_role_routing,
    promotion_blockers,
    resume_review_applications,
    select_dispatchable_issues,
)

CANDIDATE = "a" * 40
EVIDENCE = "b" * 64


def manifest():
    return Manifest(slug="feature", branch="feature/feature", workers=[
        {"id": "w1", "title": "work", "instruction": "workers/w1.md",
         "allowed_paths": ["app/**"]}])


def working_issue(identity, status="in_repair", repairs=1):
    return IssueState(
        id=identity, worker="w1", root_issue_id=identity, owner_role="code",
        status=status,
        assignments=[AssignmentState(
            repairs=repairs, failed_repairs=0, review_rounds=0,
            assessed_repairs=0, failed_repair_ordinals=[])],
    )


def finding(identity, action="rework_same_worker"):
    return Finding(id=identity, worker="w1", severity="major", action=action,
                   summary=f"Fix {identity}", root_issue_id=identity)


def verdict_file(tmp_path, role, verdict):
    path = tmp_path / f"verdict-{role}.md"
    write_markdown(path, verdict, "## Body\n\nEvidence.\n")
    return path


def accept(store, state, tmp_path, verdict):
    path = verdict_file(tmp_path, verdict.reviewer, verdict)
    return accept_review_result(
        store, state, role=verdict.reviewer, candidate_sha=CANDIDATE,
        evidence_digest=EVIDENCE, attempt_id="review-code-1-1",
        verdict=verdict, verdict_path=path, manifest=manifest(),
    )


def test_all_entries_apply_after_issue_scoped_escalation(tmp_path):
    store = StateStore(tmp_path / "state")
    identities = [chr(ord("A") + offset) for offset in range(6)]
    issues = {identity: working_issue(identity) for identity in identities}
    untouched = working_issue("G")
    state = store.save(RunState(
        run_id="run", slug="feature", phase="RUNNING", stage="REVIEW",
        issues={**issues, "G": untouched}))

    verdict = Verdict(
        reviewer="code", result="rework", candidate_sha=CANDIDATE,
        findings=[finding("A", "escalate"),
                  *[finding(identity) for identity in identities[1:]]])
    state, result = accept(store, state, tmp_path, verdict)

    application = next(iter(state.review_applications.values()))
    assert application.complete is True
    assert len(application.entries) == 6
    assert application.applied_entry_ids == [entry.entry_id for entry in application.entries]
    assert state.issues["A"].status == "escalated"
    for identity in identities[1:]:
        # Later entries were still validated and applied; the escalation did
        # not truncate the plan.
        assert state.issues[identity].status == "in_repair"
        assert state.issues[identity].assignments[-1].failed_repairs == 1
    # An unrelated entry keeps its exact prior record and counters.
    assert state.issues["G"].model_dump(mode="json") == untouched.model_dump(mode="json")
    assert "A" in (result.escalation or "")
    assert set(result.findings) == set(identities)
    assert result.affected_workers == ["w1"]


def test_plan_covers_every_entry_and_reports_issue_scoped_stops():
    issues = {identity: working_issue(identity) for identity in ("A", "B", "C")}
    plan = plan_role_routing(
        issues,
        Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                findings=[finding("A", "escalate"), finding("B"), finding("C")]),
        manifest(), "code",
    )
    assert [entry.entry_id for entry in plan.entries] == [
        "finding:A", "finding:B", "finding:C"]
    assert len(plan.escalations) == 1 and "A" in plan.escalations[0]
    assert plan.issues["A"].status == "escalated"
    assert plan.issues["B"].status == "in_repair"


def test_resolution_entries_never_become_repair_requests(tmp_path):
    store = StateStore(tmp_path / "state")
    state = store.save(RunState(
        run_id="run", slug="feature", phase="RUNNING", stage="REVIEW",
        issues={"code-1": working_issue("code-1"), "code-2": working_issue("code-2")}))
    verdict = Verdict(
        reviewer="code", result="rework", candidate_sha=CANDIDATE,
        findings=[finding("code-2")], resolved=["code-1"])
    state, result = accept(store, state, tmp_path, verdict)
    application = next(iter(state.review_applications.values()))
    kinds = {entry.issue_id: entry.kind for entry in application.entries}
    assert kinds == {"code-1": "resolution", "code-2": "finding"}
    assert state.issues["code-1"].status == "finished"
    assert result.affected_workers == ["w1"]
    # A resolution is not a rework target even though it changed lifecycle.
    assert result.findings == ["code-2"]


def test_resume_applies_recorded_entries_before_any_dispatch(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state")
    state = store.save(RunState(
        run_id="run", slug="feature", phase="RUNNING", stage="REVIEW",
        issues={"code-1": working_issue("code-1"), "code-2": working_issue("code-2")}))
    verdict = Verdict(
        reviewer="code", result="rework", candidate_sha=CANDIDATE,
        findings=[finding("code-1"), finding("code-2")])

    original = store.apply_review_entry
    calls = {"count": 0}

    def interrupted(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("crash after intent, before first entry")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "apply_review_entry", interrupted)
    with pytest.raises(RuntimeError):
        accept(store, state, tmp_path, verdict)
    monkeypatch.undo()

    saved = store.load("run")
    application = next(iter(saved.review_applications.values()))
    assert application.complete is False
    assert application.applied_entry_ids == []

    resumed = resume_review_applications(store, saved, manifest())
    application = next(iter(resumed.review_applications.values()))
    assert application.complete is True
    assert len(application.applied_entry_ids) == 2
    assert resumed.issues["code-1"].assignments[-1].failed_repairs == 1


def test_dependency_waits_do_not_dispatch_or_charge():
    dependency = type("Dependency", (), {
        "prerequisite_issue_ids": ["p1"],
        "verification_prerequisites": ["p2"],
        "shared_resource_paths": ["app/shared.py"],
    })()
    state = RunState(
        run_id="run", slug="feature", phase="RUNNING", stage="WORK",
        issues={
            "p1": working_issue("p1", status="pending", repairs=0),
            "p2": working_issue("p2", status="in_repair"),
            "dependent": working_issue("dependent", status="pending", repairs=0),
        })
    blockers = issue_dependency_blockers(
        state.issues["dependent"], state, {"dependent": dependency})
    assert any("p1" in blocker for blocker in blockers)
    selected, waiting = select_dispatchable_issues(
        [state.issues["dependent"]], state, {"dependent": dependency})
    assert selected == []
    assert waiting["dependent"] == blockers

    state.issues["p1"].status = "finished"
    state.issues["p2"].status = "finished"
    state.review_results["review-result:code:x:y"] = ReviewResult(
        role="code", candidate_sha=CANDIDATE, evidence_digest=EVIDENCE,
        attempt_id="attempt", verdict_sha256="c" * 64,
        verdict_path="round-1/verdict-code.md", result="pass",
        resolved=["p2"], accepted_at="now", transaction="review-result:code:x:y")
    selected, waiting = select_dispatchable_issues(
        [state.issues["dependent"]], state, {"dependent": dependency})
    assert selected and waiting == {}
    assert selected[0].assignments[-1].repairs == 0

    # A shared resource already active elsewhere serialises the same readiness.
    selected, waiting = select_dispatchable_issues(
        [state.issues["dependent"]], state,
        {"dependent": dependency}, active_resources={"app/shared.py"})
    assert selected == []
    assert "shared resource" in waiting["dependent"][0]


def test_escalated_issue_is_not_released_by_ordinary_resolution():
    issues = {"code-1": working_issue("code-1", status="escalated")}
    plan = plan_role_routing(
        issues,
        Verdict(reviewer="code", result="pass", candidate_sha=CANDIDATE,
                resolved=["code-1"]),
        manifest(), "code",
    )
    assert plan.issues["code-1"].status == "escalated"
    assert plan.escalations
    assert plan.entries[0].decision["escalation"]


def test_promotion_gate_names_unfinished_and_escalated_issues():
    state = RunState(
        run_id="run", slug="feature", phase="RUNNING", stage="REVIEW",
        issues={
            "done": working_issue("done", status="finished"),
            "waiting": working_issue("waiting", status="pending"),
            "stopped": working_issue("stopped", status="escalated"),
        })
    blockers = promotion_blockers(state, [])
    assert any("waiting" in blocker for blocker in blockers)
    assert any("stopped" in blocker for blocker in blockers)
    assert not any(blocker.startswith("issue done") for blocker in blockers)


def test_reporting_renders_lifecycle_and_application_progress():
    state = RunState(
        run_id="run", slug="feature", phase="RUNNING", stage="REVIEW",
        issues={"code-1": working_issue("code-1")})
    text = render_report(state, [])
    assert "code-1 (in_repair" in text
    assert "Assignment 1: 1 repairs" in text
