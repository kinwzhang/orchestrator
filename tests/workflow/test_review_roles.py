"""Independent reviewer-role ownership, validation and transaction contracts."""
import copy

import pytest

from orch.contracts import (
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
    accept_review_result,
    persist_issue_ownership,
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
