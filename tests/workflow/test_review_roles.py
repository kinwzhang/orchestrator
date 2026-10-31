"""Independent reviewer-role ownership, validation and transaction contracts."""
import copy

import pytest

from orch.contracts import (
    AssignmentState,
    ContractError,
    Finding,
    IssueState,
    Manifest,
    RunState,
    Verdict,
    write_markdown,
)
from orch.store import StateStore
from orch.workflow import (
    _legacy_pre_batch,
    accept_review_result,
    derive_repair_assessment,
    persist_issue_ownership,
    repair_assessment_evidence,
    resolve_issue_ownership,
    review_operation_key,
    review_transaction_gaps,
)

CANDIDATE = "a" * 40
EVIDENCE = "b" * 64


def manifest():
    return Manifest(slug="feature", branch="feature/feature", workers=[
        {"id": "w1", "title": "work", "instruction": "workers/w1.md", "allowed_paths": ["app/**"]}])


def state_with(issues=None):
    return RunState(run_id="run", slug="feature", phase="RUNNING", stage="REVIEW",
                    issues=issues or {})


def verdict_file(tmp_path, role, verdict):
    path = tmp_path / f"verdict-{role}.md"
    write_markdown(path, verdict, "## Body\n\nEvidence.\n")
    return path


def finding(identity, worker="w1", summary="Fix it", action="rework_same_worker", parent=None):
    return Finding(id=identity, worker=worker, severity="major", action=action,
                   summary=summary, parent_issue_id=parent, root_issue_id=identity)


def accept(store, state, tmp_path, role, verdict):
    path = verdict_file(tmp_path, role, verdict)
    return accept_review_result(
        store, state, role=role, candidate_sha=CANDIDATE, evidence_digest=EVIDENCE,
        attempt_id=f"review-{role}-1-1", verdict=verdict, verdict_path=path,
        manifest=manifest(),
    )


def test_cross_role_resolution_is_refused_before_any_state_change(tmp_path):
    store = StateStore(tmp_path / "state")
    state = state_with({
        "code-1": IssueState(id="code-1", worker="w1", root_issue_id="code-1",
                             owner_role="code", status="open"),
        "docs-1": IssueState(id="docs-1", worker="w1", root_issue_id="docs-1",
                             owner_role="docs", status="open"),
    })
    state = store.save(state)
    before = state.model_dump(mode="json")
    before_events = store.events("run")
    docs = Verdict(reviewer="docs", result="pass", candidate_sha=CANDIDATE,
                   resolved=["code-1"])
    with pytest.raises(ContractError, match="may not resolve code-1"):
        accept(store, state, tmp_path, "docs", docs)
    after = store.load("run")
    assert after.model_dump(mode="json")["issues"] == before["issues"]
    assert after.revision == state.revision
    assert store.events("run") == before_events
    assert not after.operations


def test_cross_role_finding_does_not_charge_or_reopen(tmp_path):
    store = StateStore(tmp_path / "state")
    state = state_with({
        "code-1": IssueState(id="code-1", worker="w1", root_issue_id="code-1",
                             owner_role="code", status="resolved"),
    })
    state = store.save(state)
    before = copy.deepcopy(state.issues["code-1"])
    docs = Verdict(reviewer="docs", result="rework", candidate_sha=CANDIDATE,
                   findings=[finding("code-1")])
    with pytest.raises(ContractError, match="may not report code-1"):
        accept(store, state, tmp_path, "docs", docs)
    after = store.load("run")
    assert after.issues["code-1"] == before
    assert store.load("run").revision == state.revision


def test_invalid_role_feedback_leaves_the_other_accepted_result_intact(tmp_path):
    store = StateStore(tmp_path / "state")
    state = state_with({
        "code-1": IssueState(id="code-1", worker="w1", root_issue_id="code-1",
                             owner_role="code", status="open"),
    })
    state = store.save(state)
    code = Verdict(reviewer="code", result="pass", candidate_sha=CANDIDATE,
                   resolved=["code-1"])
    state, code_result = accept(store, state, tmp_path, "code", code)
    assert state.issues["code-1"].status == "resolved"
    assert code_result.result == "pass"

    docs = Verdict(reviewer="docs", result="pass", candidate_sha=CANDIDATE,
                   resolved=["code-1"])
    with pytest.raises(ContractError, match="may not resolve"):
        accept(store, state, tmp_path, "docs", docs)
    after = store.load("run")
    assert after.review_results[code_result.transaction] == code_result
    assert not any(result.role == "docs" for result in after.review_results.values())
    assert after.issues["code-1"].status == "resolved"


def test_duplicate_role_result_is_reused_without_double_charging(tmp_path):
    store = StateStore(tmp_path / "state")
    state = state_with()
    state = store.save(state)
    code = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                   findings=[finding("code-1")])
    state, first = accept(store, state, tmp_path, "code", code)
    assert state.issues["code-1"].assignments[-1].review_rounds == 0
    state, second = accept(store, state, tmp_path, "code", code)
    assert second == first
    after = store.load("run")
    assert after.issues["code-1"].assignments[-1].review_rounds == 0
    accepted = [event for event in store.events("run")
                if event.get("kind") == "review_result_accepted"]
    assert len(accepted) == 1
    assert review_transaction_gaps(after, store.events("run")) == []


def test_conflicting_duplicate_role_result_is_refused(tmp_path):
    store = StateStore(tmp_path / "state")
    state = state_with()
    state = store.save(state)
    first = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                    findings=[finding("code-1", summary="First reason")])
    state, _ = accept(store, state, tmp_path, "code", first)
    before = store.load("run")
    second = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                     findings=[finding("code-1", summary="Different reason")])
    with pytest.raises(ContractError, match="conflicting verdicts"):
        accept(store, store.load("run"), tmp_path, "code", second)
    after = store.load("run")
    assert after.issues == before.issues
    assert after.review_results == before.review_results


@pytest.mark.parametrize("boundary", ["after-intent", "before-completion"])
def test_interrupted_role_transaction_replays_once(tmp_path, monkeypatch, boundary):
    store = StateStore(tmp_path / "state")
    state = state_with()
    state = store.save(state)
    code = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                   findings=[finding("code-1")])
    original = store.record_operation

    def interrupted(current, key, value):
        if boundary == "after-intent":
            if value.get("status") == "completed":
                return original(current, key, value)
            original(current, key, value)
            raise RuntimeError("crash after intent")
        if value.get("status") == "completed":
            raise RuntimeError("crash before completion")
        return original(current, key, value)

    monkeypatch.setattr(store, "record_operation", interrupted)
    with pytest.raises(RuntimeError):
        accept(store, state, tmp_path, "code", code)
    monkeypatch.undo()

    saved = store.load("run")
    key = review_operation_key("code", CANDIDATE, EVIDENCE)
    assert saved.operations[key]["status"] == "intent"
    assert saved.issues.get("code-1") is None or saved.issues["code-1"].assignments[-1].review_rounds == 0

    saved, result = accept(store, saved, tmp_path, "code", code)
    replay = store.load("run")
    assert replay.issues["code-1"].assignments[-1].review_rounds == 0
    assert replay.operations[key]["status"] == "completed"
    assert replay.review_results[result.transaction] == result
    accepted = [event for event in store.events("run")
                if event.get("kind") == "review_result_accepted"]
    assert len(accepted) == 1
    assert review_transaction_gaps(replay, store.events("run")) == []


def _verdict(run_dir, role, findings=(), resolved=()):
    path = run_dir / "round-1" / f"verdict-{role}.md"
    write_markdown(path, Verdict(
        reviewer=role, result="rework" if findings else "pass",
        findings=list(findings), resolved=list(resolved),
    ), "## Body\n")
    return path


def test_legacy_ownership_comes_from_finding_evidence_not_resolved_lists(tmp_path):
    state = state_with({"legacy": IssueState(id="legacy", worker="w1", root_issue_id="legacy")})
    _verdict(tmp_path, "code", findings=[finding("legacy")])
    _verdict(tmp_path, "docs", resolved=["legacy"])
    entries = resolve_issue_ownership(state, tmp_path, manifest())
    assert state.issues["legacy"].owner_role == "code"
    assert entries[0]["role"] == "code"


def test_legacy_ownership_refuses_ambiguous_and_mention_only_records(tmp_path):
    state = state_with({"legacy": IssueState(id="legacy", worker="w1", root_issue_id="legacy")})
    _verdict(tmp_path, "code", findings=[finding("legacy")])
    _verdict(tmp_path, "docs", findings=[finding("legacy")])
    with pytest.raises(ContractError, match="Cannot determine owner"):
        resolve_issue_ownership(state, tmp_path, manifest())

    state = state_with({"mention": IssueState(id="mention", worker="w1", root_issue_id="mention")})
    other = tmp_path / "other"
    other.mkdir()
    _verdict(other, "docs", resolved=["mention"])
    with pytest.raises(ContractError, match="do not establish ownership"):
        resolve_issue_ownership(state, other, manifest())


def test_workflow_generated_issue_is_system_owned(tmp_path):
    state = state_with({"check-w1": IssueState(id="check-w1", worker="w1",
                                               root_issue_id="check-w1")})
    resolve_issue_ownership(state, tmp_path, manifest())
    assert state.issues["check-w1"].owner_role == "system"


def test_ownership_migration_refusal_leaves_every_issue_untouched(tmp_path):
    state = state_with({
        "declared": IssueState(id="declared", worker="w1", root_issue_id="declared"),
        "unknown": IssueState(id="unknown", worker="w1", root_issue_id="unknown"),
    })
    _verdict(tmp_path, "code", findings=[finding("declared")])
    before = copy.deepcopy(state.issues)
    with pytest.raises(ContractError, match="Cannot determine owner of unknown"):
        resolve_issue_ownership(state, tmp_path, manifest())
    assert state.issues == before
    assert all(issue.owner_role is None for issue in state.issues.values())


def test_ownership_audit_survives_state_save_interruption_without_duplicates(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state")
    run_dir = store.run_dir("run")
    state = state_with({"legacy": IssueState(id="legacy", worker="w1", root_issue_id="legacy")})
    state = store.save(state)
    _verdict(run_dir, "code", findings=[finding("legacy")])
    def interrupted_save(current, *args, **kwargs):
        raise RuntimeError("crash after audit event before ownership save")

    monkeypatch.setattr(store, "save", interrupted_save)
    with pytest.raises(RuntimeError):
        persist_issue_ownership(store, state, run_dir, manifest())
    monkeypatch.undo()

    events = [event for event in store.events("run")
              if event.get("kind") == "issue_ownership_resolved"]
    assert len(events) == 1
    assert events[0]["issue"] == "legacy" and events[0]["role"] == "code"
    assert store.load("run").issues["legacy"].owner_role is None

    reloaded = store.load("run")
    entries, saved = persist_issue_ownership(store, reloaded, run_dir, manifest())
    assert entries == [{"issue": "legacy", "role": "code",
                        "evidence": [str(run_dir / "round-1" / "verdict-code.md")]}]
    assert saved.issues["legacy"].owner_role == "code"
    assert store.load("run").issues["legacy"].owner_role == "code"
    assert len([event for event in store.events("run")
                if event.get("kind") == "issue_ownership_resolved"]) == 1

    entries, saved = persist_issue_ownership(store, store.load("run"), run_dir, manifest())
    assert entries == []
    assert len([event for event in store.events("run")
                if event.get("kind") == "issue_ownership_resolved"]) == 1


def test_conflicting_ownership_audit_refuses_without_overwriting(tmp_path):
    store = StateStore(tmp_path / "state")
    run_dir = store.run_dir("run")
    state = state_with({"legacy": IssueState(id="legacy", worker="w1", root_issue_id="legacy")})
    state = store.save(state)
    _verdict(run_dir, "code", findings=[finding("legacy")])
    store.event("run", "issue_ownership_resolved", issue="legacy", role="docs",
                evidence=["conflicting record"])
    before = store.load("run")
    with pytest.raises(ContractError, match="refusing to overwrite"):
        persist_issue_ownership(store, before, run_dir, manifest())
    after = store.load("run")
    assert after.issues["legacy"].owner_role is None
    events = [event for event in store.events("run")
              if event.get("kind") == "issue_ownership_resolved"]
    assert len(events) == 1 and events[0]["role"] == "docs"


@pytest.mark.parametrize("order", [("docs", "code"), ("code", "docs")])
def test_multiple_owner_roles_are_refused_in_either_journal_order(tmp_path, order):
    store = StateStore(tmp_path / "state")
    run_dir = store.run_dir("run")
    state = state_with({"legacy": IssueState(id="legacy", worker="w1", root_issue_id="legacy")})
    state = store.save(state)
    _verdict(run_dir, "code", findings=[finding("legacy")])
    for role in order:
        store.event("run", "issue_ownership_resolved", issue="legacy", role=role,
                    evidence=[f"retained {role} evidence"])
    before = store.load("run")
    before_events = store.events("run")
    with pytest.raises(ContractError, match="Conflicting ownership audit for legacy"):
        persist_issue_ownership(store, before, run_dir, manifest())
    after = store.load("run")
    assert after.issues["legacy"].owner_role is None
    assert after.revision == before.revision
    assert store.events("run") == before_events


@pytest.mark.parametrize("order", [("docs", "code"), ("code", "docs")])
def test_conflict_next_to_matching_evidence_still_refuses(tmp_path, order):
    store = StateStore(tmp_path / "state")
    run_dir = store.run_dir("run")
    state = state_with({"legacy": IssueState(id="legacy", worker="w1", root_issue_id="legacy")})
    state = store.save(state)
    _verdict(run_dir, "code", findings=[finding("legacy")])
    for role in order:
        store.event("run", "issue_ownership_resolved", issue="legacy", role=role,
                    evidence=[f"retained {role} evidence"])
    before_events = store.events("run")
    with pytest.raises(ContractError, match="Conflicting ownership audit for legacy"):
        persist_issue_ownership(store, store.load("run"), run_dir, manifest())
    assert store.events("run") == before_events
    assert store.load("run").issues["legacy"].owner_role is None


def test_same_role_duplicate_audit_is_idempotent_and_adds_no_event(tmp_path):
    store = StateStore(tmp_path / "state")
    run_dir = store.run_dir("run")
    state = state_with({"legacy": IssueState(id="legacy", worker="w1", root_issue_id="legacy")})
    state = store.save(state)
    _verdict(run_dir, "code", findings=[finding("legacy")])
    for evidence in ("first retained copy", "second retained copy"):
        store.event("run", "issue_ownership_resolved", issue="legacy", role="code",
                    evidence=[evidence])
    entries, saved = persist_issue_ownership(store, store.load("run"), run_dir, manifest())
    assert entries == [{"issue": "legacy", "role": "code",
                        "evidence": [str(run_dir / "round-1" / "verdict-code.md")]}]
    assert saved.issues["legacy"].owner_role == "code"
    events = [event for event in store.events("run")
              if event.get("kind") == "issue_ownership_resolved"]
    assert len(events) == 2
    entries, _ = persist_issue_ownership(store, store.load("run"), run_dir, manifest())
    assert entries == []
    assert len([event for event in store.events("run")
                if event.get("kind") == "issue_ownership_resolved"]) == 2


@pytest.mark.parametrize("role,evidence,match", [
    ("worker", ["label"], "unsupported role"),
    ("code", [], "malformed evidence"),
    ("code", "not-a-list", "malformed evidence"),
])
def test_malformed_ownership_audit_is_refused(tmp_path, role, evidence, match):
    store = StateStore(tmp_path / "state")
    run_dir = store.run_dir("run")
    state = state_with({"legacy": IssueState(id="legacy", worker="w1", root_issue_id="legacy")})
    state = store.save(state)
    _verdict(run_dir, "code", findings=[finding("legacy")])
    store.event("run", "issue_ownership_resolved", issue="legacy", role=role,
                evidence=evidence)
    before = store.load("run")
    with pytest.raises(ContractError, match=match):
        persist_issue_ownership(store, before, run_dir, manifest())
    assert store.load("run").issues["legacy"].owner_role is None


def test_later_ownership_conflict_does_not_apply_earlier_entries(tmp_path):
    store = StateStore(tmp_path / "state")
    run_dir = store.run_dir("run")
    state = state_with({
        "first": IssueState(id="first", worker="w1", root_issue_id="first"),
        "last": IssueState(id="last", worker="w1", root_issue_id="last"),
    })
    state = store.save(state)
    _verdict(run_dir, "code", findings=[finding("first"), finding("last")])
    store.event("run", "issue_ownership_resolved", issue="last", role="docs",
                evidence=["conflicting late record"])
    before = store.load("run")
    before_events = store.events("run")
    with pytest.raises(ContractError, match="Ownership audit for last records docs"):
        persist_issue_ownership(store, before, run_dir, manifest())
    after = store.load("run")
    assert after.issues["first"].owner_role is None
    assert after.issues["last"].owner_role is None
    assert store.events("run") == before_events


def _accept_with_evidence(store, state, tmp_path, role, verdict, evidence, name=None,
                          manifest_override=None):
    path = tmp_path / f"verdict-{role}-{name or evidence[:8]}.md"
    write_markdown(path, verdict, "## Body\n\nEvidence.\n")
    return accept_review_result(
        store, state, role=role, candidate_sha=CANDIDATE, evidence_digest=evidence,
        attempt_id=f"review-{role}-1-1", verdict=verdict, verdict_path=path,
        manifest=manifest_override or manifest(),
    )


def _charged_issue(identity="code-1", repairs=1, failed_repairs=1, review_rounds=1,
                   assessed_repairs=1):
    return IssueState(
        id=identity, worker="w1", root_issue_id=identity, owner_role="code",
        status="open",
        assignments=[AssignmentState(
            repairs=repairs, failed_repairs=failed_repairs,
            review_rounds=review_rounds, assessed_repairs=assessed_repairs,
        )],
    )


def test_changed_approval_evidence_revalidates_without_recharging_repair(tmp_path):
    store = StateStore(tmp_path / "state")
    state = state_with({"code-1": _charged_issue()})
    state = store.save(state)
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])

    state, first = _accept_with_evidence(store, state, tmp_path, "code", verdict, "a" * 64)
    after_first = store.load("run")
    assert after_first.issues["code-1"].assignments[-1].failed_repairs == 1
    assert after_first.issues["code-1"].assignments[-1].review_rounds == 1

    state, second = _accept_with_evidence(store, state, tmp_path, "code", verdict, "b" * 64)
    after_second = store.load("run")
    assignment = after_second.issues["code-1"].assignments[-1]
    assert len(after_second.issues["code-1"].assignments) == 1
    assert (assignment.repairs, assignment.failed_repairs,
            assignment.review_rounds, assignment.assessed_repairs) == (1, 1, 1, 1)
    assert first.transaction != second.transaction
    assert set(after_second.review_results) == {first.transaction, second.transaction}
    assert after_second.review_results[first.transaction] == first
    assert review_transaction_gaps(after_second, store.events("run")) == []


def test_new_worker_repair_charges_once_and_second_failure_reassigns(tmp_path):
    store = StateStore(tmp_path / "state")
    state = state_with({"code-1": _charged_issue()})
    state = store.save(state)
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])

    state, _ = _accept_with_evidence(store, state, tmp_path, "code", verdict, "a" * 64)
    # The worker performs a second, genuinely new repair.
    state = store.load("run")
    state.issues["code-1"].assignments[-1].repairs = 2
    state = store.save(state)

    state, _ = _accept_with_evidence(store, state, tmp_path, "code", verdict, "b" * 64)
    after = store.load("run")
    issue = after.issues["code-1"]
    assert len(issue.assignments) == 2
    assert (issue.assignments[0].failed_repairs, issue.assignments[0].review_rounds,
            issue.assignments[0].assessed_repairs) == (2, 2, 2)
    assert issue.assignments[-1].failed_repairs == 0

    # Replaying the same accepted transaction after the reassignment is a reuse.
    _, replay = _accept_with_evidence(store, after, tmp_path, "code", verdict, "b" * 64)
    assert replay == after.review_results[
        review_operation_key("code", CANDIDATE, "b" * 64)]
    assert len(store.load("run").issues["code-1"].assignments) == 2


def _successful_repair_then_pass(tmp_path, max_failed=3, max_rounds=3):
    store = StateStore(tmp_path / "state")
    limits = manifest()
    limits.limits = limits.limits.model_copy(
        update={"max_failed_repairs_per_assignment": max_failed,
                "max_review_rounds": max_rounds})
    issue = IssueState(
        id="code-1", worker="w1", root_issue_id="code-1", owner_role="code",
        assignments=[AssignmentState(repairs=1, assessed_repairs=0,
                                     failed_repair_ordinals=[])],
    )
    state = store.save(state_with({"code-1": issue}))
    failed = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                     findings=[finding("code-1", summary="repair one incomplete")])
    state, _ = _accept_with_evidence(
        store, state, tmp_path, "code", failed, "1" * 64,
        manifest_override=limits,
    )
    state.issues["code-1"].assignments[0].repairs = 2
    state = store.save(state)
    passed = Verdict(reviewer="code", result="pass", candidate_sha=CANDIDATE,
                     resolved=["code-1"])
    state, _ = _accept_with_evidence(
        store, state, tmp_path, "code", passed, "2" * 64,
        manifest_override=limits,
    )
    return store, state, limits


def test_successful_repair_reopening_supersedes_pass_once_and_keeps_round_budget(tmp_path):
    store, state, limits = _successful_repair_then_pass(tmp_path, max_rounds=2)
    before = state.issues["code-1"].assignments[0].model_copy(deep=True)
    reopened = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                       findings=[finding("code-1", summary="same repair regressed")])
    state, result = _accept_with_evidence(
        store, state, tmp_path, "code", reopened, "3" * 64,
        manifest_override=limits,
    )
    assignment = state.issues["code-1"].assignments[0]
    assert state.issues["code-1"].status == "open"
    assert (assignment.repairs, assignment.failed_repairs, assignment.review_rounds,
            assignment.assessed_repairs, assignment.failed_repair_ordinals) == (
                2, 2, before.review_rounds, 2, [1, 2])
    decision = state.operations[result.transaction]["accounting_decisions"][0]
    assert decision == {
        "issue": "code-1", "assignment_index": 0, "repair_ordinal": 2,
        "assessment": "superseding_failure", "failed_repair_charged": True,
        "review_round_charged": False,
    }

    # A second valid finding for the same outcome is retained but cannot charge
    # the outcome or review-round allowance a second time.
    state, repeated = _accept_with_evidence(
        store, state, tmp_path, "code", reopened, "4" * 64,
        manifest_override=limits,
    )
    assignment = state.issues["code-1"].assignments[0]
    assert (assignment.failed_repairs, assignment.review_rounds,
            assignment.failed_repair_ordinals) == (2, before.review_rounds, [1, 2])
    assert state.operations[repeated.transaction]["accounting_decisions"][0]["assessment"] == "already_charged"

    # The next actual worker repair belongs to the same assignment and consumes
    # its remaining genuine repair budget once; the configured limit then routes
    # the replacement under the existing policy.
    assignment.repairs = 3
    state = store.save(state)
    state, third = _accept_with_evidence(
        store, state, tmp_path, "code", reopened, "5" * 64,
        manifest_override=limits,
    )
    issue = state.issues["code-1"]
    assert len(issue.assignments) == 2
    assert (issue.assignments[0].failed_repairs,
            issue.assignments[0].review_rounds,
            issue.assignments[0].failed_repair_ordinals) == (3, 3, [1, 2, 3])
    assert issue.assignments[1].failed_repairs == 0
    assert state.operations[third.transaction]["accounting_decisions"][0]["assessment"] == "new_failure"
    rebuilt = derive_repair_assessment(
        state.issues, repair_assessment_evidence(state), {"code-1"})
    assert rebuilt["code-1"].assignments == issue.assignments


def test_reopening_an_already_failed_repair_does_not_charge_again(tmp_path):
    store = StateStore(tmp_path / "state")
    limits = manifest()
    limits.limits = limits.limits.model_copy(
        update={"max_failed_repairs_per_assignment": 3})
    issue = IssueState(
        id="code-1", worker="w1", root_issue_id="code-1", owner_role="code",
        assignments=[AssignmentState(repairs=1, assessed_repairs=0,
                                     failed_repair_ordinals=[])],
    )
    state = store.save(state_with({"code-1": issue}))
    failure = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    state, _ = _accept_with_evidence(store, state, tmp_path, "code", failure, "a" * 64,
                                     manifest_override=limits)
    state, _ = _accept_with_evidence(
        store, state, tmp_path, "code",
        Verdict(reviewer="code", result="pass", candidate_sha=CANDIDATE,
                resolved=["code-1"]), "b" * 64, manifest_override=limits)
    state, reopened = _accept_with_evidence(
        store, state, tmp_path, "code", failure, "c" * 64, manifest_override=limits)
    assignment = state.issues["code-1"].assignments[0]
    assert (assignment.failed_repairs, assignment.review_rounds,
            assignment.failed_repair_ordinals) == (1, 1, [1])
    assert state.operations[reopened.transaction]["accounting_decisions"][0]["assessment"] == "already_charged"


def test_reopening_charge_is_attributed_to_the_replacement_assignment(tmp_path):
    store = StateStore(tmp_path / "state")
    limits = manifest()
    limits.limits = limits.limits.model_copy(
        update={"max_failed_repairs_per_assignment": 3})
    issue = IssueState(
        id="code-1", worker="w1", root_issue_id="code-1", owner_role="code",
        assignments=[AssignmentState(repairs=1, assessed_repairs=0,
                                     failed_repair_ordinals=[])],
    )
    state = store.save(state_with({"code-1": issue}))
    requested_reassignment = Verdict(
        reviewer="code", result="rework", candidate_sha=CANDIDATE,
        findings=[finding("code-1", action="reassign")])
    state, _ = _accept_with_evidence(
        store, state, tmp_path, "code", requested_reassignment, "a" * 64,
        manifest_override=limits)
    assert len(state.issues["code-1"].assignments) == 2
    state.issues["code-1"].assignments[1].repairs = 1
    state = store.save(state)
    passed = Verdict(reviewer="code", result="pass", candidate_sha=CANDIDATE,
                     resolved=["code-1"])
    state, _ = _accept_with_evidence(
        store, state, tmp_path, "code", passed, "b" * 64, manifest_override=limits)
    finding_again = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                            findings=[finding("code-1")])
    state, reopened = _accept_with_evidence(
        store, state, tmp_path, "code", finding_again, "c" * 64,
        manifest_override=limits)
    issue = state.issues["code-1"]
    assert issue.assignments[0].failed_repair_ordinals == [1]
    assert issue.assignments[1].failed_repair_ordinals == [1]
    assert [decision["assignment_index"] for decision in
            state.operations[reopened.transaction]["accounting_decisions"]] == [1]


def test_distinct_defect_keeps_separate_initial_work_accounting(tmp_path):
    store, state, limits = _successful_repair_then_pass(tmp_path)
    verdict = Verdict(
        reviewer="code", result="rework", candidate_sha=CANDIDATE,
        findings=[finding("code-1"), finding("code-2", summary="different defect")])
    state, result = _accept_with_evidence(
        store, state, tmp_path, "code", verdict, "6" * 64, manifest_override=limits)
    first, second = (state.issues[identity].assignments[0]
                     for identity in ("code-1", "code-2"))
    assert first.failed_repair_ordinals == [1, 2]
    assert second.failed_repair_ordinals == []
    decisions = {item["issue"]: item for item in
                 state.operations[result.transaction]["accounting_decisions"]}
    assert decisions["code-1"]["assessment"] == "superseding_failure"
    assert decisions["code-2"]["assessment"] == "initial_work"


def test_legacy_reopening_is_reconstructed_once_with_source_attribution(tmp_path):
    store = StateStore(tmp_path / "state")
    limits = manifest()
    failure = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    passed = Verdict(reviewer="code", result="pass", candidate_sha=CANDIDATE,
                     resolved=["code-1"])
    first_base = _charged_issue(repairs=1, failed_repairs=0, review_rounds=0,
                                assessed_repairs=0)
    first_key, first = _completed_review_operation(failure, first_base, "a" * 64)
    pass_base = _charged_issue(repairs=2, failed_repairs=1, review_rounds=1,
                               assessed_repairs=1)
    pass_key, passing = _completed_review_operation(passed, pass_base, "b" * 64)
    pass_base.status = "resolved"
    pass_base.assignments[-1].assessed_repairs = 2
    pass_base.assignments[-1].review_rounds = 2
    reopen_base = pass_base.model_copy(deep=True)
    reopen_key, reopening = _completed_review_operation(failure, reopen_base, "c" * 64)
    current = _charged_issue(repairs=2, failed_repairs=1, review_rounds=2,
                             assessed_repairs=2)
    state = state_with({"code-1": current})
    state.operations = {first_key: first, pass_key: passing, reopen_key: reopening}
    state = store.save(state)

    state, result = _accept_with_evidence(
        store, state, tmp_path, "code", failure, "d" * 64, manifest_override=limits)
    assignment = state.issues["code-1"].assignments[0]
    assert (assignment.failed_repairs, assignment.failed_repair_ordinals) == (2, [1, 2])
    reconciliation = state.operations[result.transaction]["legacy_reconciliations"]
    assert reconciliation == [{
        "issue": "code-1", "assignment_index": 0, "repair_ordinal": 2,
        "source_transactions": [reopen_key],
    }]
    state, _ = _accept_with_evidence(
        store, state, tmp_path, "code", failure, "e" * 64, manifest_override=limits)
    assignment = state.issues["code-1"].assignments[0]
    assert (assignment.failed_repairs, assignment.failed_repair_ordinals) == (2, [1, 2])


def test_ambiguous_legacy_reopening_refuses_before_mutation(tmp_path):
    store = StateStore(tmp_path / "state")
    finding_verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                              findings=[finding("code-1")])
    resolved_base = _charged_issue(repairs=2, failed_repairs=1, review_rounds=1,
                                   assessed_repairs=2)
    resolved_base.status = "resolved"
    key, operation = _completed_review_operation(finding_verdict, resolved_base, "a" * 64)
    issue = _charged_issue(repairs=2, failed_repairs=1, review_rounds=2,
                           assessed_repairs=2)
    issue.assignments[-1].failed_repair_ordinals = None
    state = state_with({"code-1": issue})
    state.operations = {key: operation}
    state = store.save(state)
    before, events = store.load("run"), store.events("run")
    with pytest.raises(ContractError, match="no retained successful assessment"):
        _accept_with_evidence(store, before, tmp_path, "code", finding_verdict, "b" * 64)
    after = store.load("run")
    assert after.model_dump(mode="json") == before.model_dump(mode="json")
    assert store.events("run") == events
    assert not any("b" * 64 in key for key in after.operations)


@pytest.mark.parametrize("boundary", ["after-intent", "after-issue-save", "before-completion"])
def test_superseding_reopening_crash_replay_keeps_one_charge(tmp_path, monkeypatch, boundary):
    store, state, limits = _successful_repair_then_pass(tmp_path)
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    original_record = store.record_operation
    original_save = store.save

    def interrupted_record(current, key, value):
        if boundary == "after-intent" and value.get("status") == "intent":
            original_record(current, key, value)
            raise RuntimeError("crash after accounting intent")
        if boundary == "before-completion" and value.get("status") == "completed":
            raise RuntimeError("crash before transaction completion")
        return original_record(current, key, value)

    def interrupted_save(current, *args, **kwargs):
        result = original_save(current, *args, **kwargs)
        if (boundary == "after-issue-save"
                and any(assignment.failed_repair_ordinals == [1, 2]
                        for issue in current.issues.values()
                        for assignment in issue.assignments)):
            raise RuntimeError("crash after issue accounting save")
        return result

    monkeypatch.setattr(store, "record_operation", interrupted_record)
    monkeypatch.setattr(store, "save", interrupted_save)
    with pytest.raises(RuntimeError):
        _accept_with_evidence(store, state, tmp_path, "code", verdict, "7" * 64,
                              manifest_override=limits)
    monkeypatch.undo()
    replay_state, result = _accept_with_evidence(
        store, store.load("run"), tmp_path, "code", verdict, "7" * 64,
        manifest_override=limits)
    assignment = replay_state.issues["code-1"].assignments[0]
    assert (assignment.failed_repairs, assignment.review_rounds,
            assignment.failed_repair_ordinals) == (2, 2, [1, 2])
    assert replay_state.operations[result.transaction]["accounting_decisions"][0]["assessment"] == "superseding_failure"
    assert len([event for event in store.events("run")
                if event.get("kind") == "review_result_accepted"
                and event.get("transaction") == result.transaction]) == 1
    assert review_transaction_gaps(replay_state, store.events("run")) == []


@pytest.mark.parametrize("boundary", ["after-intent", "before-completion"])
def test_revalidation_crash_replay_charges_a_new_repair_once(tmp_path, monkeypatch, boundary):
    store = StateStore(tmp_path / "state")
    state = state_with({"code-1": _charged_issue(
        repairs=2, failed_repairs=1, review_rounds=1, assessed_repairs=1)})
    state = store.save(state)
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    original = store.record_operation

    def interrupted(current, key, value):
        if boundary == "after-intent":
            if value.get("status") == "completed":
                return original(current, key, value)
            original(current, key, value)
            raise RuntimeError("crash after intent")
        if value.get("status") == "completed":
            raise RuntimeError("crash before completion")
        return original(current, key, value)

    monkeypatch.setattr(store, "record_operation", interrupted)
    with pytest.raises(RuntimeError):
        _accept_with_evidence(store, state, tmp_path, "code", verdict, "c" * 64)
    monkeypatch.undo()

    saved = store.load("run")
    if boundary == "before-completion":
        # The plan was saved before the crash; replay must not apply it twice.
        assert len(saved.issues["code-1"].assignments) == 2
        assert saved.issues["code-1"].assignments[0].failed_repairs == 2
    else:
        # Only the intent is durable; no counter was applied yet.
        assert len(saved.issues["code-1"].assignments) == 1
        assert saved.issues["code-1"].assignments[0].failed_repairs == 1
    saved, result = _accept_with_evidence(store, saved, tmp_path, "code", verdict, "c" * 64)
    replay = store.load("run")
    assignment = replay.issues["code-1"].assignments[0]
    assert (assignment.failed_repairs, assignment.review_rounds,
            assignment.assessed_repairs) == (2, 2, 2)
    assert len(replay.issues["code-1"].assignments) == 2
    assert replay.review_results[result.transaction] == result
    assert len([event for event in store.events("run")
                if event.get("kind") == "review_result_accepted"]) == 1
    assert review_transaction_gaps(replay, store.events("run")) == []


def test_contradictory_legacy_repair_evidence_refuses_before_writes(tmp_path):
    store = StateStore(tmp_path / "state")
    state = state_with({"code-1": _charged_issue(
        repairs=1, failed_repairs=2, review_rounds=2)})
    state = store.save(state)
    before = store.load("run")
    before_events = store.events("run")
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    with pytest.raises(ContractError, match="Counters are inconsistent"):
        _accept_with_evidence(store, state, tmp_path, "code", verdict, "d" * 64)
    after = store.load("run")
    assert after.model_dump(mode="json") == before.model_dump(mode="json")
    assert store.events("run") == before_events
    assert not any(key.startswith("review-result:") for key in after.operations)


def test_retained_legacy_result_is_not_rebound_to_changed_approval(tmp_path):
    store = StateStore(tmp_path / "state")
    state = state_with({"code-1": _charged_issue()})
    state = store.save(state)
    legacy_path = tmp_path / "verdict-code-legacy.md"
    legacy = Verdict(reviewer="code", result="rework", findings=[finding("code-1")])
    write_markdown(legacy_path, legacy, "## Body\n\nRetained legacy evidence.\n")
    state, retained = accept_review_result(
        store, state, role="code", candidate_sha=CANDIDATE, evidence_digest="a" * 64,
        attempt_id="review-code-2-2", verdict=legacy, verdict_path=legacy_path,
        manifest=manifest(), require_candidate_binding=False,
    )
    assert retained.binding == "legacy-attempt"
    assert state.issues["code-1"].assignments[-1].failed_repairs == 1

    # A later approval snapshot needs a new verdict bound to the candidate. It is
    # a separate transaction; the retained result is never rewritten or rebound.
    current_path = tmp_path / "verdict-code-current.md"
    current = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    write_markdown(current_path, current, "## Body\n\nCurrent approval evidence.\n")
    state, fresh = accept_review_result(
        store, state, role="code", candidate_sha=CANDIDATE, evidence_digest="b" * 64,
        attempt_id="review-code-2-3", verdict=current, verdict_path=current_path,
        manifest=manifest(),
    )
    after = store.load("run")
    assert fresh.binding == "candidate-sha"
    assert fresh.transaction != retained.transaction
    assert after.review_results[retained.transaction] == retained
    assert after.review_results[fresh.transaction] == fresh
    assignment = after.issues["code-1"].assignments[-1]
    assert (assignment.failed_repairs, assignment.review_rounds,
            assignment.assessed_repairs) == (1, 1, 1)


def _completed_review_operation(verdict, base_issue, evidence, candidate=CANDIDATE):
    key = review_operation_key("code", candidate, evidence)
    return key, {
        "status": "completed",
        "role": "code",
        "candidate_sha": candidate,
        "evidence_digest": evidence,
        "attempt_id": "review-code-1-1",
        "verdict_sha256": "0" * 64,
        "verdict_path": f"/tmp/{evidence}.md",
        "binding": "candidate-sha",
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "verdict": verdict.model_dump(mode="json"),
        "base_issues": {base_issue.id: base_issue.model_dump(mode="json")},
    }


def test_completed_review_evidence_derives_the_assessed_repair(tmp_path):
    store = StateStore(tmp_path / "state")
    issue = _charged_issue(repairs=1, failed_repairs=1, review_rounds=1,
                           assessed_repairs=None)
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    base = _charged_issue(repairs=1, failed_repairs=0, review_rounds=0,
                          assessed_repairs=0)
    key, operation = _completed_review_operation(verdict, base, "a" * 64)
    state = state_with({"code-1": issue})
    state.operations = {key: operation}
    state = store.save(state)

    state, result = _accept_with_evidence(store, state, tmp_path, "code", verdict, "b" * 64)
    after = store.load("run")
    assignment = after.issues["code-1"].assignments[-1]
    assert (assignment.repairs, assignment.failed_repairs,
            assignment.review_rounds, assignment.assessed_repairs) == (1, 1, 1, 1)
    assert result.transaction != key
    assert after.review_results[result.transaction] == result


def test_duplicate_legacy_failure_charge_is_refused_under_non_default_limit(tmp_path):
    store = StateStore(tmp_path / "state")
    issue = _charged_issue(repairs=2, failed_repairs=2, review_rounds=2,
                           assessed_repairs=None)
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    base_first = _charged_issue(repairs=1, failed_repairs=0, review_rounds=0,
                                assessed_repairs=0)
    base_repeat = _charged_issue(repairs=1, failed_repairs=1, review_rounds=1,
                                 assessed_repairs=1)
    first_key, first = _completed_review_operation(verdict, base_first, "a" * 64)
    repeat_key, repeat = _completed_review_operation(verdict, base_repeat, "b" * 64)
    state = state_with({"code-1": issue})
    state.operations = {first_key: first, repeat_key: repeat}
    state = store.save(state)
    before = store.load("run")

    limits = manifest()
    limits.limits = limits.limits.model_copy(
        update={"max_failed_repairs_per_assignment": 3})
    with pytest.raises(ContractError, match="only 1 distinct unsuccessful"):
        _accept_with_evidence(store, store.load("run"), tmp_path, "code", verdict,
                              "c" * 64, manifest_override=limits)
    after = store.load("run")
    assert after.model_dump(mode="json") == before.model_dump(mode="json")
    assert store.events("run") == []
    assert not any("c" * 64 in key for key in after.operations)


@pytest.mark.parametrize("mutation,match", [
    ({"assessed_repairs": 0, "failed_repairs": 0}, "retained reviews assessed outcome 1"),
    ({"failed_repairs": 0}, "retained reviews show 1 distinct unsuccessful"),
])
def test_explicit_mark_disagreeing_with_evidence_refuses(tmp_path, mutation, match):
    store = StateStore(tmp_path / "state")
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    base = _charged_issue(repairs=1, failed_repairs=0, review_rounds=0,
                          assessed_repairs=0)
    key, operation = _completed_review_operation(verdict, base, "a" * 64)
    issue = _charged_issue(repairs=1, failed_repairs=1, review_rounds=1,
                           assessed_repairs=1)
    issue.assignments[-1] = issue.assignments[-1].model_copy(update=mutation)
    state = state_with({"code-1": issue})
    state.operations = {key: operation}
    state = store.save(state)
    before = store.load("run")
    with pytest.raises(ContractError, match=match):
        _accept_with_evidence(store, store.load("run"), tmp_path, "code", verdict,
                              "c" * 64)
    after = store.load("run")
    assert after.model_dump(mode="json") == before.model_dump(mode="json")
    assert store.events("run") == []


def test_legacy_pre_batch_restores_assessment_mark_and_recharges(tmp_path):
    post = _charged_issue(repairs=1, failed_repairs=1, review_rounds=1,
                          assessed_repairs=1)
    state = state_with({"code-1": post})
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    pre, removed, restored = _legacy_pre_batch(state, [verdict], {"code-1"}, {})
    assert removed == []
    assert restored == ["code-1"]
    assignment = pre["code-1"].assignments[-1]
    assert (assignment.repairs, assignment.failed_repairs,
            assignment.review_rounds, assignment.assessed_repairs) == (1, 0, 0, 0)

    store = StateStore(tmp_path / "state")
    saved = store.save(state_with(pre))
    saved, result = _accept_with_evidence(store, saved, tmp_path, "code", verdict, "e" * 64)
    after = store.load("run")
    assignment = after.issues["code-1"].assignments[-1]
    assert (assignment.failed_repairs, assignment.review_rounds,
            assignment.assessed_repairs) == (1, 1, 1)
    assert after.review_results[result.transaction] == result

    _, replay = _accept_with_evidence(store, store.load("run"), tmp_path, "code",
                                      verdict, "e" * 64)
    assert replay == result
    assert store.load("run").issues["code-1"].assignments[-1].failed_repairs == 1


def test_legacy_pre_batch_refuses_when_evidence_cannot_establish_mark():
    post = _charged_issue(repairs=1, failed_repairs=1, review_rounds=1,
                          assessed_repairs=1)
    state = state_with({"code-1": post})
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    evidence = {("code-1", 0): {1: {"findings": ["prior-transaction"],
                                    "resolved": []}}}
    before = state.model_dump(mode="json")
    with pytest.raises(ContractError, match="understate the assessed history"):
        _legacy_pre_batch(state, [verdict], {"code-1"}, evidence)
    assert state.model_dump(mode="json") == before


@pytest.mark.parametrize("boundary", ["after-intent", "before-completion"])
def test_reconstructed_batch_reacceptance_is_crash_safe(tmp_path, monkeypatch, boundary):
    post = _charged_issue(repairs=1, failed_repairs=1, review_rounds=1,
                          assessed_repairs=1)
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    pre, _, _ = _legacy_pre_batch(state_with({"code-1": post}), [verdict],
                                  {"code-1"}, {})
    store = StateStore(tmp_path / "state")
    state = store.save(state_with(pre))
    original = store.record_operation

    def interrupted(current, key, value):
        if boundary == "after-intent":
            if value.get("status") == "completed":
                return original(current, key, value)
            original(current, key, value)
            raise RuntimeError("crash after intent")
        if value.get("status") == "completed":
            raise RuntimeError("crash before completion")
        return original(current, key, value)

    monkeypatch.setattr(store, "record_operation", interrupted)
    with pytest.raises(RuntimeError):
        _accept_with_evidence(store, state, tmp_path, "code", verdict, "f" * 64)
    monkeypatch.undo()

    _, result = _accept_with_evidence(store, store.load("run"), tmp_path, "code",
                                      verdict, "f" * 64)
    replay = store.load("run")
    assignment = replay.issues["code-1"].assignments[-1]
    assert (assignment.failed_repairs, assignment.review_rounds,
            assignment.assessed_repairs) == (1, 1, 1)
    assert replay.review_results[result.transaction] == result
    assert len([event for event in store.events("run")
                if event.get("kind") == "review_result_accepted"]) == 1
    assert review_transaction_gaps(replay, store.events("run")) == []


@pytest.mark.parametrize("missing_mark", [False, True])
def test_initial_ordinal_zero_finding_then_first_repair_charges_once(tmp_path, missing_mark):
    store = StateStore(tmp_path / "state")
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    state = state_with({"code-1": _charged_issue(
        repairs=0, failed_repairs=0, review_rounds=0, assessed_repairs=None)})
    state = store.save(state)

    state, _ = _accept_with_evidence(store, state, tmp_path, "code", verdict, "a" * 64)
    after_first = store.load("run")
    assignment = after_first.issues["code-1"].assignments[-1]
    assert (assignment.repairs, assignment.failed_repairs,
            assignment.review_rounds, assignment.assessed_repairs) == (0, 0, 0, 0)

    # The worker performs the first actual repair.
    after_first.issues["code-1"].assignments[-1].repairs = 1
    if missing_mark:
        after_first.issues["code-1"].assignments[-1].assessed_repairs = None
    after_first = store.save(after_first)

    state, _ = _accept_with_evidence(store, after_first, tmp_path, "code", verdict, "b" * 64)
    after_second = store.load("run")
    issue = after_second.issues["code-1"]
    assert len(issue.assignments) == 1
    assignment = issue.assignments[-1]
    assert (assignment.repairs, assignment.failed_repairs,
            assignment.review_rounds, assignment.assessed_repairs) == (1, 1, 1, 1)

    # Revalidating the first actual repair does not charge it again.
    _, third = _accept_with_evidence(store, state, tmp_path, "code", verdict, "c" * 64)
    after_third = store.load("run")
    assignment = after_third.issues["code-1"].assignments[-1]
    assert (assignment.failed_repairs, assignment.review_rounds) == (1, 1)
    _, replay = _accept_with_evidence(store, after_third, tmp_path, "code", verdict, "c" * 64)
    assert replay == third
    assert len(store.load("run").issues["code-1"].assignments) == 1

    # A genuinely new second unsuccessful repair still exhausts the allowance.
    repaired = store.load("run")
    repaired.issues["code-1"].assignments[-1].repairs = 2
    repaired = store.save(repaired)
    state, _ = _accept_with_evidence(store, repaired, tmp_path, "code", verdict, "d" * 64)
    after_fourth = store.load("run")
    issue = after_fourth.issues["code-1"]
    assert len(issue.assignments) == 2
    assert (issue.assignments[0].failed_repairs,
            issue.assignments[0].review_rounds) == (2, 2)


def test_repeated_approval_revalidation_of_unrepaired_work_does_not_charge(tmp_path):
    store = StateStore(tmp_path / "state")
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    state = state_with({"code-1": _charged_issue(
        repairs=0, failed_repairs=0, review_rounds=0, assessed_repairs=None)})
    state = store.save(state)
    for evidence in ("a" * 64, "b" * 64, "c" * 64):
        state, _ = _accept_with_evidence(store, state, tmp_path, "code", verdict, evidence)
        assignment = store.load("run").issues["code-1"].assignments[-1]
        assert (assignment.repairs, assignment.failed_repairs,
                assignment.review_rounds, assignment.assessed_repairs) == (0, 0, 0, 0)
    after = store.load("run")
    assert len(after.issues["code-1"].assignments) == 1
    assert len([result for result in after.review_results.values()
                if result.role == "code"]) == 3


def test_ordinal_zero_finding_does_not_mask_a_failed_counter(tmp_path):
    store = StateStore(tmp_path / "state")
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    base = _charged_issue(repairs=0, failed_repairs=0, review_rounds=0,
                          assessed_repairs=0)
    key, operation = _completed_review_operation(verdict, base, "a" * 64)
    issue = _charged_issue(repairs=1, failed_repairs=1, review_rounds=1,
                           assessed_repairs=None)
    state = state_with({"code-1": issue})
    state.operations = {key: operation}
    state = store.save(state)
    before = store.load("run")

    with pytest.raises(ContractError, match="only 0 distinct unsuccessful"):
        _accept_with_evidence(store, store.load("run"), tmp_path, "code", verdict,
                              "b" * 64)
    after = store.load("run")
    assert after.model_dump(mode="json") == before.model_dump(mode="json")
    assert store.events("run") == []


def test_ordinal_zero_evidence_still_validates_explicit_assessment(tmp_path):
    store = StateStore(tmp_path / "state")
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    base = _charged_issue(repairs=0, failed_repairs=0, review_rounds=0,
                          assessed_repairs=0)
    key, operation = _completed_review_operation(verdict, base, "a" * 64)
    issue = _charged_issue(repairs=1, failed_repairs=1, review_rounds=1,
                           assessed_repairs=1)
    state = state_with({"code-1": issue})
    state.operations = {key: operation}
    state = store.save(state)
    before = store.load("run")

    with pytest.raises(ContractError, match="retained reviews assessed outcome 0"):
        _accept_with_evidence(store, store.load("run"), tmp_path, "code", verdict,
                              "b" * 64)
    after = store.load("run")
    assert after.model_dump(mode="json") == before.model_dump(mode="json")
    assert store.events("run") == []


def test_missing_mark_with_ordinal_zero_evidence_revalidates_without_charge(tmp_path):
    store = StateStore(tmp_path / "state")
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-1")])
    base = _charged_issue(repairs=0, failed_repairs=0, review_rounds=0,
                          assessed_repairs=0)
    key, operation = _completed_review_operation(verdict, base, "a" * 64)
    issue = _charged_issue(repairs=0, failed_repairs=0, review_rounds=0,
                           assessed_repairs=None)
    state = state_with({"code-1": issue})
    state.operations = {key: operation}
    state = store.save(state)

    state, first = _accept_with_evidence(store, state, tmp_path, "code", verdict, "b" * 64)
    assignment = store.load("run").issues["code-1"].assignments[-1]
    assert (assignment.repairs, assignment.failed_repairs,
            assignment.review_rounds, assignment.assessed_repairs) == (0, 0, 0, 0)
    _, second = _accept_with_evidence(store, state, tmp_path, "code", verdict, "c" * 64)
    assignment = store.load("run").issues["code-1"].assignments[-1]
    assert (assignment.repairs, assignment.failed_repairs,
            assignment.review_rounds, assignment.assessed_repairs) == (0, 0, 0, 0)
    assert first.transaction != second.transaction


def test_live_shaped_ordinal_zero_then_first_repair_review(tmp_path):
    store = StateStore(tmp_path / "state")
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE,
                      findings=[finding("code-w01-05")])
    state = state_with({"code-w01-05": _charged_issue(
        identity="code-w01-05", repairs=0, failed_repairs=0, review_rounds=0,
        assessed_repairs=None)})
    state = store.save(state)

    state, _ = _accept_with_evidence(store, state, tmp_path, "code", verdict, "a" * 64)
    assignment = store.load("run").issues["code-w01-05"].assignments[-1]
    assert (assignment.repairs, assignment.failed_repairs,
            assignment.review_rounds, assignment.assessed_repairs) == (0, 0, 0, 0)

    repaired = store.load("run")
    repaired.issues["code-w01-05"].assignments[-1].repairs = 1
    repaired = store.save(repaired)
    state, _ = _accept_with_evidence(store, repaired, tmp_path, "code", verdict, "b" * 64)
    after = store.load("run")
    issue = after.issues["code-w01-05"]
    assert len(issue.assignments) == 1
    assignment = issue.assignments[-1]
    assert (assignment.repairs, assignment.failed_repairs,
            assignment.review_rounds, assignment.assessed_repairs) == (1, 1, 1, 1)
