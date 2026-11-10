"""Independent whole-chain guarantee tests for the review lifecycle/application plan.

Scenarios S01-S10 of ``outputs/w03/1-matrix.md`` (approved packet
``20261109_review_lifecycle_delivery``) bound to the committed W02 reducer interface
(source ``e9ea61f``), plus S11 combined lifecycle x product coverage bound to the
carry-forward S1/S2 interfaces (models preferences, sanitized approval diagnostics,
dependency/promotion gates). Every test uses isolated temporary evidence, synthetic
issue registers and deterministic verdict files. No worker, model, network or runner
is started, and no live run evidence is written.

Tests never assert an internal implementation shape: they drive the public acceptance
entry point (``accept_review_result``), resume (``resume_review_applications``), the
evidence authority (``classify_review_evidence`` / ``require_authoritative_evidence``),
dependency scheduling and promotion gates, and observe the durable register, journal and
per-review application record afterwards.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path

import pytest

from orch.cli import parser
from orch.config import Config, effective_feature, resolve_config
from orch.contracts import (
    AssignmentState,
    ContractError,
    DependencyRecord,
    EvidenceConflictError,
    FeatureSpec,
    Finding,
    FindingDependency,
    HumanDisposition,
    IssueState,
    LifecycleTransitionError,
    Limits,
    Manifest,
    ModelSpec,
    RepairOutcomeCharge,
    ReviewerProvenance,
    ReviewResult,
    RoleModels,
    RunState,
    TransitionGuard,
    UnverifiableEvidenceError,
    Verdict,
    apply_human_disposition,
    apply_lifecycle_transition,
    approval_hashes,
    assess_repair_outcome,
    classify_review_evidence,
    create_pending_issue,
    create_system_issue,
    detect_approval_changes,
    finish_system_issue,
    migrate_v1_state,
    read_markdown,
    require_authoritative_evidence,
    require_operational_schema,
    write_markdown,
)
from orch.git_ops import git
from orch.models import run_models
from orch.store import StateStore
from orch.workflow import (
    ApprovalRequired,
    Engine,
    accept_review_result,
    load_validated_dependencies,
    plan_role_routing,
    promotion_blockers,
    resolve_uncertain_dispatch,
    resume_review_applications,
    review_transaction_gaps,
    select_dispatchable_issues,
)

CANDIDATE_A = "a" * 40
CANDIDATE_B = "b" * 40
EVIDENCE_1 = "1" * 64
EVIDENCE_2 = "2" * 64
EVIDENCE_3 = "3" * 64


class Crash(BaseException):
    """Failpoint that broad ``except Exception`` recovery paths cannot swallow."""


# --------------------------------------------------------------------------- harness


def manifest(**limit_overrides) -> Manifest:
    limits = Limits(**{**Limits().model_dump(), **limit_overrides})
    return Manifest(slug="feature", branch="feature/feature", limits=limits, workers=[
        {"id": "w1", "title": "work", "instruction": "workers/w1.md",
         "allowed_paths": ["app/**"]}])


def issue(identity, *, status="in_repair", repairs=1, failed=0, rounds=0,
          assessed=0, ordinals=None, owner="code", worker="w1") -> IssueState:
    return IssueState(
        id=identity, worker=worker, root_issue_id=identity, owner_role=owner,
        status=status,
        assignments=[AssignmentState(
            repairs=repairs, failed_repairs=failed, review_rounds=rounds,
            assessed_repairs=assessed,
            failed_repair_ordinals=[] if ordinals is None else list(ordinals))],
    )


def finding(identity, action="rework_same_worker") -> Finding:
    return Finding(id=identity, worker="w1", severity="major", action=action,
                   summary=f"Fix {identity}", root_issue_id=identity)


def new_store(tmp_path, name="run") -> StateStore:
    return StateStore(tmp_path / f"state-{name}")


def seed(store: StateStore, issues: dict[str, IssueState]) -> RunState:
    return store.save(RunState(run_id="run", slug="feature", phase="RUNNING",
                               stage="REVIEW", issues=issues))


def verdict_file(tmp_path: Path, verdict: Verdict) -> Path:
    path = tmp_path / f"verdict-{verdict.reviewer}-{len(list(tmp_path.glob('verdict-*.md')))}.md"
    write_markdown(path, verdict, "## Body\n\nRetained reviewer evidence.\n")
    return path


def accept(store, state, verdict, *, evidence, path=None, tmp_path=None,
           candidate=None, attempt="review-code-1-1", manifest_value=None):
    if path is None:
        path = verdict_file(tmp_path, verdict)
    return accept_review_result(
        store, state, role=verdict.reviewer,
        candidate_sha=candidate or verdict.candidate_sha,
        evidence_digest=evidence, attempt_id=attempt,
        verdict=verdict, verdict_path=path,
        manifest=manifest_value or manifest(),
    )


def run_bytes(store: StateStore, run_id="run") -> tuple[bytes, bytes]:
    run_dir = store.run_dir(run_id)
    journal = run_dir / "events.jsonl"
    return ((run_dir / "state.json").read_bytes(),
            journal.read_bytes() if journal.exists() else b"")


def issue_projection(state: RunState) -> dict:
    return {
        identity: {
            "status": item.status,
            "assignments": [
                (assignment.repairs, assignment.failed_repairs,
                 assignment.review_rounds, assignment.assessed_repairs,
                 tuple(assignment.failed_repair_ordinals or []))
                for assignment in item.assignments
            ],
            "outcomes": [
                (outcome.repair_ordinal, outcome.outcome,
                 outcome.charged_failed_repair, outcome.charged_review_round)
                for assignment in item.assignments for outcome in assignment.outcomes
            ],
        }
        for identity, item in sorted(state.issues.items())
    }


def application_projection(state: RunState) -> list:
    return sorted(
        (
            application.application_id,
            tuple(entry.entry_id for entry in application.entries),
            tuple((entry.entry_id, entry.state,
                   None if entry.decision is None else sorted(entry.decision))
                  for entry in application.entries),
            application.complete,
        )
        for application in state.review_applications.values()
    )


def event_projection(events: list[dict]) -> list:
    return sorted(
        (event["kind"], event.get("entry_id") or "",
         event.get("application_id") or "", event.get("decision_digest") or "")
        for event in events
    )


def semantic(state: RunState, events: list[dict]) -> tuple:
    """Timestamp-free golden projection used for crash/replay equivalence."""
    return (issue_projection(state), application_projection(state),
            event_projection(events))


def decisions(application) -> dict[str, dict]:
    return {entry.entry_id: entry.decision for entry in application.entries}


def digest_of(decision: dict) -> str:
    payload = json.dumps(decision, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def crash_at(store: StateStore, method: str, *, call=None, key_prefix=None,
             before=True):
    """Inject a ``Crash`` at one durable store boundary.

    ``call`` is 1-based; ``key_prefix`` triggers on the operation key instead.
    ``before`` selects "write did not happen"; otherwise the write happens and the
    follow-up does not.
    """
    original = getattr(store, method)
    calls = {"n": 0}

    def wrapper(*args, **kwargs):
        calls["n"] += 1
        if key_prefix is not None:
            trigger = len(args) > 1 and isinstance(args[1], str) \
                and args[1].startswith(key_prefix)
        else:
            trigger = calls["n"] == call
        if trigger:
            if not before:
                original(*args, **kwargs)
            raise Crash(method)
        return original(*args, **kwargs)

    setattr(store, method, wrapper)
    try:
        yield calls
    finally:
        setattr(store, method, original)


# ------------------------------------------------------- S01 whole A-F batch


def test_s01_escalating_batch_applies_every_entry_and_preserves_untouched(tmp_path):
    """G01/G02/G09/G13/G16/AC2/AC4: A escalates; B-F still validate and apply.

    Entry order is the verdict's finding order followed by its resolution order:
    A (explicit stop), B (ordinal-zero), C (already charged), F (new failure),
    D (resolution), E (validated no-change).
    """
    store = new_store(tmp_path, "s01")
    issues = {
        "A": issue("A"),
        "B": issue("B", status="pending", repairs=0, assessed=None),
        "C": issue("C"),
        "D": issue("D"),
        "E": issue("E"),
        "F": issue("F"),
        "G": issue("G", status="pending", repairs=0, assessed=None),
    }
    state = seed(store, issues)
    untouched = state.issues["G"].model_dump(mode="json")

    # Round 1 charges A and C once and finishes E, so round 2 has real retained
    # evidence for an already-charged outcome (C) and a validated no-change
    # resolution (E).
    state, first = accept(
        store, state,
        Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                findings=[finding("A"), finding("C")], resolved=["E"]),
        evidence=EVIDENCE_1, tmp_path=tmp_path, attempt="review-code-1-1")
    assert first.escalation is None
    assert state.issues["E"].status == "finished"

    # The worker was dispatched again for A; C and E were not.
    state.issues["A"].assignments[-1].repairs = 2
    state = store.save(state)
    checks = store.events("run")

    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                      findings=[finding("A", "escalate"), finding("B"),
                                finding("C"), finding("F")],
                      resolved=["D", "E"])
    state, result = accept(store, state, verdict, evidence=EVIDENCE_2,
                           tmp_path=tmp_path, attempt="review-code-2-1")

    application = state.review_applications[
        state.operations[result.transaction]["application_id"]]
    assert [entry.entry_id for entry in application.entries] == [
        "finding:A", "finding:B", "finding:C", "finding:F",
        "resolution:D", "resolution:E"]
    assert application.complete is True
    assert application.fully_applied is True
    assert len(application.entries) == 6

    applied = decisions(application)
    # A is stopped and charged exactly once for its actual second repair.
    assert state.issues["A"].status == "escalated"
    assignment = state.issues["A"].assignments[-1]
    assert assignment.failed_repairs == 2
    assert assignment.failed_repair_ordinals == [1, 2]
    assert applied["finding:A"]["escalation"]
    assert result.escalation and "A" in result.escalation
    # B is ordinal-zero initial work: no failed-repair charge, no round consumed.
    assert state.issues["B"].status == "pending"
    assert state.issues["B"].assignments[-1].repairs == 0
    assert state.issues["B"].assignments[-1].failed_repairs == 0
    assert state.issues["B"].assignments[-1].review_rounds == 0
    assert applied["finding:B"]["accounting"]["assessment"] == "initial_work"
    # C is presented again but already charged: no second charge.
    assert applied["finding:C"]["accounting"]["assessment"] == "already_charged"
    assert state.issues["C"].assignments[-1].failed_repairs == 1
    assert state.issues["C"].assignments[-1].failed_repair_ordinals == [1]
    # F is a genuine new failure, charged once.
    assert applied["finding:F"]["accounting"]["failed_repair_charged"] is True
    assert state.issues["F"].assignments[-1].failed_repairs == 1
    # D is a resolution, never a repair request; E is a validated no-change.
    assert applied["resolution:D"]["kind"] == "resolution"
    assert state.issues["D"].status == "finished"
    assert state.issues["D"].assignments[-1].failed_repairs == 0
    assert state.issues["D"].assignments[-1].review_rounds == 1
    assert applied["resolution:E"]["accounting"]["review_round_charged"] is False
    assert state.issues["E"].status == "finished"
    assert state.issues["E"].assignments[-1].failed_repairs == 0
    # Untouched entry keeps its exact prior record and counters.
    assert state.issues["G"].model_dump(mode="json") == untouched

    events = store.events("run")
    audit = [event for event in events
             if event.get("kind") == "review_entry_applied"
             and event.get("application_id") == application.application_id]
    completed = [event for event in events
                 if event.get("kind") == "review_application_completed"
                 and event.get("application_id") == application.application_id]
    assert len(audit) == 6
    assert len(completed) == 1
    assert len([event for event in events
                if event.get("kind") == "review_result_accepted"
                and event.get("transaction") == result.transaction]) == 1
    # Application itself never launches a worker.
    assert not any(event.get("kind") == "dispatch_intent" for event in events[len(checks):])

    # Independent ready work is dispatchable while A's issue-scoped stop blocks
    # promotion; a dependent of A waits with an explicit reason and no charge.
    state.issues["I"] = issue("I", status="pending", repairs=0, assessed=None)
    state.issues["J"] = issue("J", status="pending", repairs=0, assessed=None)
    dependencies = {
        "J": FindingDependency(
            dependent_issue_id="J", prerequisite_issue_ids=["A"],
            rationale="needs A's committed interface"),
    }
    selected, waiting = select_dispatchable_issues(
        [state.issues["I"], state.issues["J"]], state, dependencies)
    assert [item.id for item in selected] == ["I"]
    assert "semantic prerequisite A is escalated" in waiting["J"][0]
    assert state.issues["J"].assignments[-1].repairs == 0
    blockers = promotion_blockers(state, events)
    assert any("A" in blocker and "escalated" in blocker for blocker in blockers)
    assert any("B" in blocker and "pending" in blocker for blocker in blockers)
    assert review_transaction_gaps(state, events) == []


# --------------------------------------------- S02 crash boundaries and replay


def _batch_issues(identities):
    return {identity: issue(identity) for identity in identities}


def _clean_batch(tmp, name, identities):
    store = new_store(tmp, name)
    state = seed(store, _batch_issues(identities))
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                      findings=[finding(identity) for identity in identities])
    state, _ = accept(store, state, verdict, evidence=EVIDENCE_2, tmp_path=tmp)
    return store, state


@pytest.mark.parametrize("count", [1, 2, 6])
@pytest.mark.parametrize("boundary", ["entry-save", "entry-audit"])
def test_s02_per_entry_crash_resumes_to_golden_projection(tmp_path, boundary, count):
    """G03/CB-A2(i)/CB-A3(i)/CB-A4(i): every entry position, 1/2/6-entry batches."""
    identities = [chr(ord("A") + offset) for offset in range(count)]
    golden_store, golden_state = _clean_batch(tmp_path, "golden", identities)
    golden = semantic(golden_state, golden_store.events("run"))

    for index in range(count):
        store = new_store(tmp_path, f"{boundary}-{index}")
        state = seed(store, _batch_issues(identities))
        verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                          findings=[finding(identity) for identity in identities])
        path = verdict_file(tmp_path, verdict)
        method = "apply_review_entry" if boundary == "entry-save" \
            else "record_review_entry_audit"
        with crash_at(store, method, call=index + 1), pytest.raises(Crash):
            accept(store, state, verdict, evidence=EVIDENCE_2, path=path,
                   attempt=f"review-code-{index}-1")

        interrupted = store.load("run")
        application = next(iter(interrupted.review_applications.values()))
        states = [entry.state for entry in application.entries]
        events = store.events("run")
        audits = [event for event in events if event.get("kind") == "review_entry_applied"]
        if boundary == "entry-save":
            # Durable prefix: entries before i applied; i and later were not.
            assert states[:index] == ["applied"] * index, (boundary, index, states)
            assert states[index:] == ["not_applied"] * (count - index)
            assert len(audits) == index
        else:
            # Entry i is applied but its audit event is missing.
            assert states[:index + 1] == ["applied"] * (index + 1)
            assert states[index + 1:] == ["not_applied"] * (count - index - 1)
            assert len(audits) == index
        assert application.complete is False
        assert not any(event.get("kind") == "review_application_completed"
                       for event in events)

        resumed = resume_review_applications(store, interrupted, manifest())
        events = store.events("run")
        application = next(iter(resumed.review_applications.values()))
        assert application.complete is True
        # Exactly one audit event per entry, reconstructing any missing event
        # from the durable decision.
        by_entry = {}
        for event in events:
            if event.get("kind") == "review_entry_applied":
                by_entry.setdefault(event["entry_id"], []).append(event)
        assert sorted(by_entry) == sorted(entry.entry_id for entry in application.entries)
        for entry in application.entries:
            assert len(by_entry[entry.entry_id]) == 1
            assert by_entry[entry.entry_id][0]["decision_digest"] == digest_of(entry.decision)
        assert semantic(resumed, events) == golden
        assert review_transaction_gaps(resumed, events) == []


def test_s02_crash_before_intent_leaves_no_operation_and_retry_creates_one(tmp_path):
    """CB-A0: no intent and no mutation; the retry creates exactly one intent."""
    store = new_store(tmp_path, "intent")
    state = seed(store, _batch_issues(["A", "B"]))
    before = issue_projection(state)
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                      findings=[finding("A"), finding("B")])
    path = verdict_file(tmp_path, verdict)

    with crash_at(store, "record_operation", key_prefix="review-result:"), pytest.raises(Crash):
        accept(store, state, verdict, evidence=EVIDENCE_2, path=path)
    interrupted = store.load("run")
    assert not any(key.startswith("review-result:") for key in interrupted.operations)
    assert interrupted.review_applications == {}
    assert issue_projection(interrupted) == before

    accepted, result = accept(store, interrupted, verdict, evidence=EVIDENCE_2,
                              path=path, attempt="review-code-2-1")
    assert len([key for key in accepted.operations
                if key.startswith("review-result:")]) == 1
    application = accepted.review_applications[
        accepted.operations[result.transaction]["application_id"]]
    assert application.complete is True


def test_s02_crash_after_final_audit_resumes_completion_without_recharge(tmp_path):
    """CB-A5/CB-A6: all entries audited but completion absent; resume completes once."""
    store = new_store(tmp_path, "completion")
    state = seed(store, _batch_issues(["A", "B"]))
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                      findings=[finding("A"), finding("B")])
    path = verdict_file(tmp_path, verdict)
    with crash_at(store, "complete_review_application", call=1), pytest.raises(Crash):
        accept(store, state, verdict, evidence=EVIDENCE_2, path=path)

    interrupted = store.load("run")
    application = next(iter(interrupted.review_applications.values()))
    assert application.fully_applied is True
    assert application.complete is False
    events = store.events("run")
    assert len([event for event in events
                if event.get("kind") == "review_entry_applied"]) == 2
    assert not any(event.get("kind") == "review_application_completed"
                   for event in events)

    resumed = resume_review_applications(store, interrupted, manifest())
    events = store.events("run")
    assert next(iter(resumed.review_applications.values())).complete is True
    assert len([event for event in events
                if event.get("kind") == "review_application_completed"]) == 1
    assert len([event for event in events
                if event.get("kind") == "review_result_accepted"]) == 1
    # A completed application is never re-applied; replay from the completed
    # operation returns the retained result without a new event or charge.
    revision = resumed.revision
    again = resume_review_applications(store, resumed, manifest())
    assert again.revision == revision
    replayed, result = accept(store, again, verdict, evidence=EVIDENCE_2, path=path)
    assert replayed.revision == revision
    assert result.transaction in replayed.review_results
    assert len([event for event in store.events("run")
                if event.get("kind") == "review_entry_applied"]) == 2


def test_s02_contradictory_audit_is_refused_without_mutation(tmp_path):
    """CB-A8/E13: a missing event is reconstructed; a contradictory one is refused."""
    store = new_store(tmp_path, "contradiction")
    state = seed(store, _batch_issues(["A", "B"]))
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                      findings=[finding("A"), finding("B")])
    path = verdict_file(tmp_path, verdict)
    with crash_at(store, "complete_review_application", call=1), pytest.raises(Crash):
        accept(store, state, verdict, evidence=EVIDENCE_2, path=path)

    interrupted = store.load("run")
    application = next(iter(interrupted.review_applications.values()))
    entry = application.entries[0]
    # Replace the real audit event with one whose digest contradicts the
    # durable decision: a single, contradictory claim.
    journal = store.run_dir("run") / "events.jsonl"
    kept = [event for event in store.events("run")
            if not (event.get("kind") == "review_entry_applied"
                    and event.get("entry_id") == entry.entry_id)]
    journal.write_text("".join(json.dumps(event) + "\n" for event in kept),
                       encoding="utf-8")
    store.event("run", "review_entry_applied",
                application_id=application.application_id, entry_id=entry.entry_id,
                transaction=application.transaction,
                reviewer_role=application.reviewer_role,
                decision=entry.decision, decision_digest="0" * 64)
    state_bytes, journal_bytes = run_bytes(store)

    with pytest.raises(EvidenceConflictError, match="disagrees with its durable decision"):
        resume_review_applications(store, interrupted, manifest())
    assert run_bytes(store) == (state_bytes, journal_bytes)
    assert store.load("run").review_applications[
        application.application_id].complete is False
    assert not any(event.get("kind") == "review_application_completed"
                   for event in store.events("run"))


def test_s02_duplicate_acceptance_and_completion_are_detected(tmp_path):
    """CB-A9/G18b/E14: duplicate evidence is not silently accepted as idempotent."""
    store = new_store(tmp_path, "duplicates")
    state = seed(store, _batch_issues(["A"]))
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                      findings=[finding("A")])
    state, result = accept(store, state, verdict, evidence=EVIDENCE_2, tmp_path=tmp_path)
    application = next(iter(state.review_applications.values()))
    accepted_event = next(event for event in store.events("run")
                          if event.get("kind") == "review_result_accepted")

    store.event("run", "review_result_accepted",
                transaction=result.transaction, role="code",
                candidate_sha=CANDIDATE_A, evidence_digest=EVIDENCE_2,
                accounting_decisions=accepted_event.get("accounting_decisions"))
    gaps = review_transaction_gaps(state, store.events("run"))
    assert any("acceptance audit event missing or duplicated" in gap for gap in gaps)
    assessment = classify_review_evidence(
        state, application.application_id, operation="acceptance",
        events=store.events("run"))
    assert assessment.classification == "conflict"
    assert "multiple acceptance audit events" in assessment.conflicts

    # A second completion event for the same application is contradictory.
    store.event("run", "review_application_completed",
                application_id=application.application_id,
                transaction=application.transaction,
                applied_entry_ids=application.applied_entry_ids,
                completed_at=application.completed_at)
    with pytest.raises(EvidenceConflictError, match="multiple completion events"):
        store.complete_review_application(state, application.application_id)


# ------------------------------------------- S03 lifecycle transition conformance


@pytest.mark.parametrize("source,target,guard", [
    ("pending", "in_repair", TransitionGuard(
        identity_valid=True, owner_lineage_valid=True, dispatch_intent=True,
        dependencies_ready=True, allowance_available=True,
        process_ownership_known=True)),
    ("in_repair", "in_repair", TransitionGuard(subsequent_attempt=True)),
    ("in_repair", "finished", TransitionGuard(reviewer_acceptance=True)),
    ("finished", "pending", TransitionGuard(
        reopening_valid=True, historical_attribution=True, remaining_work=True)),
    ("finished", "escalated", TransitionGuard(
        reopening_valid=True, stop_boundary=True)),
    ("pending", "escalated", TransitionGuard(explicit_escalation=True)),
    ("in_repair", "escalated", TransitionGuard(allowance_exhausted=True)),
    ("escalated", "finished", TransitionGuard(
        scoped_human_acceptance=True, owner_disposition=True,
        acceptance_evidence=True)),
    ("escalated", "pending", TransitionGuard(
        human_continuation=True, continuation_scope=True,
        continuation_allowance=True)),
])
def test_s03_legal_transitions_require_their_guard(source, target, guard):
    """G11: legal transitions produce the expected status, guard and history."""
    item = issue("T", status=source, repairs=1, assessed=0)
    counters = item.assignments[0].model_dump(mode="json")
    transition = apply_lifecycle_transition(item, target, guard,
                                            evidence=["authority:review"])
    assert item.status == target
    assert transition.from_status == source and transition.to_status == target
    assert transition.guard
    assert item.lifecycle_history[-1] == transition
    assert item.assignments[0].model_dump(mode="json") == counters


@pytest.mark.parametrize("source,target,guard", [
    ("pending", "finished", TransitionGuard(reviewer_acceptance=True)),
    ("in_repair", "pending", TransitionGuard(explicit_escalation=True)),
    ("finished", "in_repair", TransitionGuard(subsequent_attempt=True)),
    ("escalated", "in_repair", TransitionGuard(subsequent_attempt=True)),
    ("pending", "pending", TransitionGuard()),
    ("finished", "finished", TransitionGuard()),
])
def test_s03_illegal_transitions_refuse_before_mutation(source, target, guard):
    """G12: unlisted transitions and unjustified no-ops leave the record untouched."""
    item = issue("T", status=source, repairs=1, assessed=0)
    before = item.model_dump(mode="json")
    with pytest.raises(LifecycleTransitionError):
        apply_lifecycle_transition(item, target, guard)
    assert item.model_dump(mode="json") == before


def test_s03_illegal_reducer_effect_leaves_state_and_journal_unchanged(tmp_path):
    """G12: a pending issue cannot be finished by a passing reviewer verdict."""
    store = new_store(tmp_path, "illegal-reducer")
    state = seed(store, {"P": issue("P", status="pending", repairs=0, assessed=None)})
    path = verdict_file(tmp_path, Verdict(
        reviewer="code", result="pass", candidate_sha=CANDIDATE_A, resolved=["P"]))
    before = run_bytes(store)
    with pytest.raises(ContractError, match="pending, not in repair"):
        accept(store, state,
               Verdict(reviewer="code", result="pass", candidate_sha=CANDIDATE_A,
                       resolved=["P"]),
               evidence=EVIDENCE_2, path=path)
    assert run_bytes(store) == before


def test_s03_human_disposition_and_system_ownership_stay_guarded():
    """G11/G21: continuation returns to pending, deferral is labelled, checks stay system."""
    escalated = issue("E", status="escalated", repairs=2, failed=1, assessed=2,
                      ordinals=[1])
    counters = escalated.assignments[0].model_dump(mode="json")
    transition = apply_human_disposition(escalated, HumanDisposition(
        kind="continuation", issue_id="E", scope="complete remaining fix",
        evidence=["human-gate:2026-11-10"], allowance_disposition="retain counters",
        dependencies=["I1"]))
    assert escalated.status == "pending"
    assert escalated.assignments[0].model_dump(mode="json") == counters
    assert transition.from_status == "escalated"
    assert "dependency:I1" in transition.evidence

    pending = issue("D", status="pending", repairs=0, assessed=None)
    deferred = apply_human_disposition(pending, HumanDisposition(
        kind="deferral", issue_id="D", scope="ship doc-only change",
        evidence=["human-gate:2026-11-10"], allowance_disposition="defer",
        deferred_stage="REVIEW"))
    assert pending.status == "finished"
    assert "deferred-stage:REVIEW" in deferred.evidence

    with pytest.raises(ValueError, match="must name the approved stage"):
        HumanDisposition(kind="deferral", issue_id="D", scope="x",
                         evidence=["e"], allowance_disposition="defer")

    check = create_system_issue("check-w1", "w1", root_issue_id="check-w1",
                                summary="check", evidence=["worker-check:w1:1"])
    assert check.status == "pending" and check.owner_role == "system"
    finish_system_issue(check, check_evidence=["worker-check:w1:1:pass"])
    assert check.status == "finished"
    with pytest.raises(LifecycleTransitionError, match="only a workflow-owned system issue"):
        finish_system_issue(issue("code-1"), check_evidence=["forged"])
    with pytest.raises(ContractError, match="owned by system"):
        plan_role_routing(
            {"check-w1": check},
            Verdict(reviewer="code", result="pass", candidate_sha=CANDIDATE_A,
                    resolved=["check-w1"]),
            manifest(), "code")


def test_s03_reassignment_and_depth_boundaries_escalate_without_reset():
    """AC1/AC5/G11: reassignments retain history; exceeded allowance/depth escalates."""
    limits = Limits()
    assert (limits.max_repair_attempts, limits.max_failed_repairs_per_assignment,
            limits.max_reassignments) == (3, 2, 1)

    plan = plan_role_routing(
        {"R": issue("R", repairs=1, assessed=0)},
        Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                findings=[finding("R", "reassign")]),
        manifest(), "code")
    retained = plan.issues["R"].assignments
    assert len(retained) == 2
    assert (retained[0].repairs, retained[0].failed_repairs) == (1, 1)
    assert (retained[1].repairs, retained[1].failed_repairs) == (0, 0)
    assert plan.escalations == []

    two_assignments = IssueState(
        id="R", worker="w1", root_issue_id="R", owner_role="code", status="in_repair",
        assignments=[
            AssignmentState(repairs=1, failed_repairs=1, assessed_repairs=1,
                            failed_repair_ordinals=[1]),
            AssignmentState(repairs=3, failed_repairs=1, assessed_repairs=2,
                            failed_repair_ordinals=[1]),
        ])
    stopped = plan_role_routing(
        {"R": two_assignments},
        Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                findings=[finding("R", "reassign")]),
        manifest(), "code")
    assert stopped.issues["R"].status == "escalated"
    assert len(stopped.issues["R"].assignments) == 2
    assert stopped.escalations and "replacement allowance" in stopped.escalations[0]

    root = IssueState(id="root", worker="w1", root_issue_id="root", owner_role="code",
                      status="in_repair", depth=2,
                      assignments=[AssignmentState(repairs=1, assessed_repairs=0,
                                                   failed_repair_ordinals=[])])
    child = Finding(id="child", worker="w1", severity="major",
                    action="rework_same_worker", summary="fix", root_issue_id="root",
                    parent_issue_id="root")
    depth = plan_role_routing(
        {"root": root},
        Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                findings=[child]),
        manifest(), "code")
    assert depth.issues["root"].status == "escalated"
    assert depth.escalations and "depth limit" in depth.escalations[0]


# ------------------------------- S04 reopening, attribution and charged outcomes


def test_s04_valid_reopening_supersedes_once_and_preserves_history(tmp_path):
    """G07/G08/AC2/AC6: finished -> pending charges the producing repair once."""
    store = new_store(tmp_path, "reopen")
    state = seed(store, {"E": issue("E")})
    state, _ = accept(
        store, state,
        Verdict(reviewer="code", result="pass", candidate_sha=CANDIDATE_A,
                resolved=["E"]),
        evidence=EVIDENCE_1, tmp_path=tmp_path, attempt="review-code-1-1")
    assert state.issues["E"].status == "finished"
    assert state.issues["E"].assignments[-1].assessed_repairs == 1

    state, result = accept(
        store, state,
        Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                findings=[finding("E")]),
        evidence=EVIDENCE_2, tmp_path=tmp_path, attempt="review-code-2-1")
    reopened = state.issues["E"]
    assignment = reopened.assignments[-1]
    assert reopened.status == "pending"
    assert (assignment.failed_repairs, assignment.failed_repair_ordinals) == (1, [1])
    assert [outcome.outcome for outcome in assignment.outcomes] == [
        "unsuccessful", "successful"]
    # The earlier successful assessment is retained, not erased.
    assert {outcome.outcome for outcome in assignment.outcomes} == {
        "successful", "unsuccessful"}
    assert len([event for event in store.events("run")
                if event.get("kind") == "review_entry_applied"
                and event.get("application_id")
                == state.operations[result.transaction]["application_id"]]) == 1

    # Presenting the identical outcome again is a replay: no second charge.
    revision = state.revision
    replayed, repeated = accept(store, state,
                                Verdict(reviewer="code", result="rework",
                                        candidate_sha=CANDIDATE_A,
                                        findings=[finding("E")]),
                                evidence=EVIDENCE_2, tmp_path=tmp_path,
                                attempt="review-code-2-1")
    assert repeated.transaction == result.transaction
    assert replayed.revision == revision
    assert replayed.issues["E"].assignments[-1].failed_repairs == 1
    assert len([outcome for outcome in replayed.issues["E"]
                .assignments[-1].outcomes if outcome.outcome == "unsuccessful"]) == 1


def test_s04_historical_assignment_attribution_is_exact_or_refused():
    """G10: charge the producing assignment/ordinal, never a newer unrelated one."""
    provenance = ReviewerProvenance(
        reviewer_role="code", attempt_id="review-code-9-1",
        candidate_sha=CANDIDATE_A, verdict_sha256="f" * 64,
        evidence_digest=EVIDENCE_3, verdict_path="/tmp/review/verdict-code.md",
        transaction="review-result:code:" + CANDIDATE_A + ":" + EVIDENCE_3,
        ownership_valid=True, dispatch_evidence=True, completion_evidence=True)
    two = IssueState(
        id="H", worker="w1", root_issue_id="H", owner_role="code", status="in_repair",
        assignments=[
            AssignmentState(repairs=1, assessed_repairs=0, failed_repair_ordinals=[]),
            AssignmentState(repairs=1, assessed_repairs=0, failed_repair_ordinals=[]),
        ])
    before = two.model_dump(mode="json")
    with pytest.raises(ContractError, match="ambiguous across assignments"):
        assess_repair_outcome(two, "H", provenance)
    assert two.model_dump(mode="json") == before

    charge = assess_repair_outcome(two, "H", provenance, assignment_index=0)
    assert isinstance(charge, RepairOutcomeCharge)
    assert charge.assignment_index == 0 and charge.repair_ordinal == 1
    assert charge.failed_repair_charged is True
    older, newer = two.assignments
    assert (older.failed_repairs, older.failed_repair_ordinals) == (1, [1])
    assert (newer.failed_repairs, newer.failed_repair_ordinals) == (0, [])


def test_s04_ordinal_zero_is_initial_work_not_a_failed_repair():
    """G09: ordinal-zero work creates no failed-repair charge or allowance use."""
    provenance = ReviewerProvenance(
        reviewer_role="code", attempt_id="attempt-1", candidate_sha=CANDIDATE_A,
        verdict_sha256="f" * 64, evidence_digest=EVIDENCE_3,
        verdict_path="/tmp/verdict-code.md", transaction="review-result:code:x:z",
        ownership_valid=True, dispatch_evidence=True, completion_evidence=True)
    fresh = create_pending_issue("N", "w1", root_issue_id="N", owner_role="code")
    charge = assess_repair_outcome(fresh, "N", provenance)
    assert charge.assessment == "initial_work"
    assert charge.failed_repair_charged is False
    assert fresh.assignments[-1].failed_repairs == 0
    assert fresh.assignments[-1].failed_repair_ordinals == []


# --------------------------- S05 duplicate review / revalidation / repeated findings


def test_s05_duplicate_review_is_idempotent_and_conflicts_are_refused(tmp_path):
    """G05/AC3/G18b: identical replay is free; a conflicting verdict is refused."""
    store = new_store(tmp_path, "duplicate-review")
    state = seed(store, {"A": issue("A")})
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                      findings=[finding("A")])
    path = verdict_file(tmp_path, verdict)
    state, first = accept(store, state, verdict, evidence=EVIDENCE_2, path=path)
    revision = state.revision
    events_before = len(store.events("run"))
    counters = state.issues["A"].assignments[-1].model_dump(mode="json")

    replayed, again = accept(store, state, verdict, evidence=EVIDENCE_2, path=path)
    assert again.transaction == first.transaction
    assert replayed.revision == revision
    assert len(store.events("run")) == events_before
    assert replayed.issues["A"].assignments[-1].model_dump(mode="json") == counters

    conflicting = verdict_file(tmp_path, Verdict(
        reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
        findings=[finding("A"), finding("B")]))
    with pytest.raises(ContractError, match="conflicting verdicts"):
        accept(store, replayed, Verdict(
            reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
            findings=[finding("A"), finding("B")]),
            evidence=EVIDENCE_2, path=conflicting)
    assert len(store.events("run")) == events_before


def test_s05_repeated_findings_do_not_release_an_escalated_issue():
    """G05/AC3: repeated ordinary findings/resolutions add no outcome or release."""
    escalated = issue("E", status="escalated", repairs=2, failed=1, assessed=2,
                      ordinals=[1])
    before = issue_projection(RunState(run_id="r", slug="f", issues={"E": escalated}))
    plan = plan_role_routing(
        {"E": escalated},
        Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                findings=[finding("E")]),
        manifest(), "code")
    assert plan.issues["E"].status == "escalated"
    assert plan.issues["E"].assignments[-1].model_dump(mode="json") == \
        escalated.assignments[-1].model_dump(mode="json")
    assert plan.escalations and "cannot reopen" in plan.escalations[0]
    assert issue_projection(RunState(run_id="r", slug="f",
                                     issues={"E": escalated})) == before

    resolution = plan_role_routing(
        {"E": escalated},
        Verdict(reviewer="code", result="pass", candidate_sha=CANDIDATE_A,
                resolved=["E"]),
        manifest(), "code")
    assert resolution.issues["E"].status == "escalated"
    assert resolution.escalations and "cannot silently release" in \
        resolution.escalations[0]


def test_s05_retained_verdict_is_bound_to_its_evidence_digest(tmp_path):
    """G06/G18e: a new approval/evidence set cannot rebind a retained verdict."""
    store = new_store(tmp_path, "rebind")
    state = seed(store, {"A": issue("A")})
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                      findings=[finding("A")])
    path = verdict_file(tmp_path, verdict)
    state, first = accept(store, state, verdict, evidence=EVIDENCE_2, path=path)
    state, second = accept(store, state, verdict, evidence=EVIDENCE_3,
                           path=path, attempt="review-code-2-1")
    assert first.transaction != second.transaction
    assert first.evidence_digest == EVIDENCE_2
    assert second.evidence_digest == EVIDENCE_3
    # Replaying the old digest still returns the original retained result and
    # never substitutes the newer evidence binding.
    replayed, again = accept(store, state, verdict, evidence=EVIDENCE_2, path=path)
    assert again.transaction == first.transaction
    assert replayed.review_results[first.transaction].evidence_digest == EVIDENCE_2
    assert replayed.review_results[second.transaction].evidence_digest == EVIDENCE_3


# ------------------------------------------- S06 evidence classes and defects


@pytest.fixture
def verified_application(tmp_path):
    store = new_store(tmp_path, "evidence")
    state = seed(store, {"A": issue("A"), "B": issue("B")})
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                      findings=[finding("A")], resolved=["B"])
    state, result = accept(store, state, verdict, evidence=EVIDENCE_2,
                           tmp_path=tmp_path)
    application = state.review_applications[
        state.operations[result.transaction]["application_id"]]
    return store, state, application


def test_s06_verified_evidence_is_the_only_authoritative_class(verified_application):
    """E01/G20a: verified evidence is consumed; unbound evidence is diagnostic only."""
    store, state, application = verified_application
    events = store.events("run")
    assessment = require_authoritative_evidence(
        state, application.application_id, operation="acceptance", events=events)
    assert assessment.classification == "verified"
    assert assessment.authoritative is True

    unbound = state.model_copy(deep=True)
    unbound.review_results.clear()
    classification = classify_review_evidence(
        unbound, application.application_id, operation="acceptance", events=events)
    assert classification.classification == "recomputed_unbound"
    assert classification.authoritative is False
    with pytest.raises(UnverifiableEvidenceError, match="without an original binding"):
        require_authoritative_evidence(
            unbound, application.application_id, operation="acceptance", events=events)


def test_s06_missing_inputs_and_missing_digests_are_distinct(verified_application):
    """E03/E04/G20b: a missing prerequisite and a missing proof are different facts."""
    store, state, application = verified_application
    events = store.events("run")

    missing_input = state.model_copy(deep=True)
    missing_input.review_results.clear()
    # Remove the digest too so the missing-input classification is isolated.
    digestless = application.model_copy(update={"evidence_digest": EVIDENCE_2})
    missing_input.review_applications[application.application_id] = digestless
    assessment = classify_review_evidence(
        missing_input, application.application_id, operation="acceptance",
        events=[event for event in events
                if event.get("kind") != "review_result_accepted"])
    assert assessment.classification == "unverifiable"
    assert "acceptance audit event" in assessment.missing_inputs
    assert assessment.missing_digests == []

    missing_digest = state.model_copy(deep=True)
    missing_digest.review_results.clear()
    missing_digest.review_applications[application.application_id] = \
        application.model_copy(update={"verdict_sha256": ""})
    assessment = classify_review_evidence(
        missing_digest, application.application_id, operation="acceptance",
        events=events)
    assert assessment.classification == "unverifiable"
    assert "verdict digest" in assessment.missing_digests
    assert "verdict digest" not in " ".join(assessment.missing_inputs)
    with pytest.raises(UnverifiableEvidenceError, match="verdict digest"):
        require_authoritative_evidence(
            missing_digest, application.application_id, operation="acceptance",
            events=events)


def test_s06_conflict_mismatch_and_duplicates_stop_authoritative_use(
        verified_application):
    """E05/E07/E09/E12/G20c: every contradiction is a conflict and refuses."""
    store, state, application = verified_application
    events = store.events("run")
    result = state.review_results[application.transaction]

    mismatched = state.model_copy(deep=True)
    mismatched.review_results = {
        key: value.model_copy(update={"candidate_sha": CANDIDATE_B})
        for key, value in mismatched.review_results.items()}
    assessment = classify_review_evidence(
        mismatched, application.application_id, operation="acceptance", events=events)
    assert assessment.classification == "conflict"
    assert any("candidate binding disagrees" in item for item in assessment.conflicts)
    with pytest.raises(EvidenceConflictError):
        require_authoritative_evidence(
            mismatched, application.application_id, operation="acceptance",
            events=events)

    ownership = state.model_copy(deep=True)
    ownership.issues["A"] = ownership.issues["A"].model_copy(
        update={"owner_role": "docs"})
    assessment = classify_review_evidence(
        ownership, application.application_id, operation="recovery", events=events)
    assert assessment.classification == "conflict"
    assert any("owned by docs" in item for item in assessment.conflicts)

    duplicated = state.model_copy(deep=True)
    duplicated.review_results[application.transaction] = result
    duplicate_events = [*events, {**events[-1], "id": 100000,
                                  "kind": "review_result_accepted",
                                  "transaction": application.transaction}]
    assessment = classify_review_evidence(
        state, application.application_id, operation="acceptance",
        events=duplicate_events)
    assert assessment.classification == "conflict"
    assert "multiple acceptance audit events" in assessment.conflicts

    # A recycled audit event whose decision digest disagrees is contradictory.
    cloned = state.model_copy(deep=True)
    application_state = cloned.review_applications[application.application_id]
    entry = application_state.entries[0]
    contradictory = [*events, {
        "id": 100001, "kind": "review_entry_applied",
        "application_id": application_state.application_id,
        "entry_id": entry.entry_id, "decision_digest": "0" * 64}]
    assessment = classify_review_evidence(
        cloned, application_state.application_id, operation="acceptance",
        events=contradictory)
    assert assessment.classification == "conflict"


def test_s06_reordered_or_duplicated_entry_evidence_is_refused(verified_application):
    """E08/G18c: order is resolved by the recorded contract, never by position guess."""
    _store, _state, application = verified_application
    reordered = application.model_dump(mode="json")
    reordered["entries"] = [entry.model_dump(mode="json")
                            for entry in application.entries]
    reordered["entries"][0]["order_index"] = 1
    reordered["entries"][1]["order_index"] = 0
    with pytest.raises(ValueError, match="contiguous"):
        type(application).model_validate(reordered)

    duplicated = application.model_dump(mode="json")
    duplicated["entries"] = [application.entries[0].model_dump(mode="json") | {
        "order_index": index} for index in range(len(application.entries))]
    with pytest.raises(ValueError, match="Duplicate review entry identity"):
        type(application).model_validate(duplicated)


def test_s06_corrupt_journal_is_an_explicit_refusal(verified_application):
    """E15: an unparsable journal is refused, not guessed or rewritten."""
    store, _state, _application = verified_application
    journal = store.run_dir("run") / "events.jsonl"
    with journal.open("a", encoding="utf-8") as stream:
        stream.write("{not json}\n")
    with pytest.raises(ContractError, match="corrupt event journal"):
        store.events("run")


# ------------------------- S07 dependency dispatch, serialisation, promotion, process


def dependency_manifest(plan: Path) -> Manifest:
    return Manifest(
        slug="feature", branch="feature/feature", dependency_record="dependencies.md",
        workers=[
            {"id": "w1", "title": "work", "instruction": "workers/w1.md",
             "allowed_paths": ["app/**"]},
            {"id": "w2", "title": "docs", "instruction": "workers/w2.md",
             "allowed_paths": ["docs/**"]},
        ])


def test_s07_dispatch_waits_serialises_and_promotion_blocks(tmp_path):
    """G13/G14/G15/G16: explicit waits, no allowance use, shared-path serialisation."""
    plan = tmp_path / "plan"
    plan.mkdir()
    record = DependencyRecord(
        plan_version="v1", source_verdicts=["review-result:code:x:y"],
        dependencies=[
            FindingDependency(dependent_issue_id="dependent",
                              prerequisite_issue_ids=["independent"],
                              rationale="needs the committed interface"),
            FindingDependency(dependent_issue_id="verified",
                              verification_prerequisites=["rev"],
                              rationale="needs assembled review evidence"),
            FindingDependency(dependent_issue_id="shared-a",
                              shared_resource_paths=["app/shared.py"],
                              rationale="same file"),
            FindingDependency(dependent_issue_id="shared-b",
                              shared_resource_paths=["app/shared.py"],
                              rationale="same file"),
        ])
    write_markdown(plan / "dependencies.md", record, "## Dependencies\n")
    store = new_store(tmp_path, "deps")
    state = seed(store, {
        "independent": issue("independent", status="pending", repairs=0,
                             assessed=None),
        "dependent": issue("dependent", status="pending", repairs=0, assessed=None),
        "verified": issue("verified", status="pending", repairs=0, assessed=None),
        "rev": issue("rev", status="finished", repairs=1, assessed=1),
        "shared-a": issue("shared-a", status="pending", repairs=0, assessed=None),
        "shared-b": issue("shared-b", status="pending", repairs=0, assessed=None),
        "escalated": issue("escalated", status="escalated", repairs=2, failed=1,
                           assessed=2, ordinals=[1]),
    })
    dependencies = load_validated_dependencies(
        dependency_manifest(plan), plan, state)
    assert set(dependencies) == {"dependent", "verified", "shared-a", "shared-b"}

    candidates = [state.issues[identity] for identity in
                  ("independent", "dependent", "verified", "shared-a", "shared-b")]
    before = {item.id: item.assignments[-1].model_dump(mode="json")
              for item in candidates}
    selected, waiting = select_dispatchable_issues(
        candidates, state, dependencies)
    assert [item.id for item in selected] == ["independent", "shared-a"]
    assert any("semantic prerequisite independent" in reason
               for reason in waiting["dependent"])
    assert any("assembled evidence" in reason for reason in waiting["verified"])
    assert any("shared resource serialised" in reason for reason in waiting["shared-b"])
    assert {item.id: item.assignments[-1].model_dump(mode="json")
            for item in candidates} == before

    # Assembled review evidence releases the verification dependent.
    state.review_results["review-result:code:x:y"] = ReviewResult(
        role="code", candidate_sha=CANDIDATE_A, evidence_digest=EVIDENCE_2,
        attempt_id="review-code-1-1", verdict_sha256="c" * 64,
        verdict_path="round-1/verdict-code.md", result="pass",
        resolved=["rev"], accepted_at="recorded",
        transaction="review-result:code:x:y")
    selected, _waiting = select_dispatchable_issues(
        candidates, state, dependencies)
    assert "verified" in [item.id for item in selected]

    blockers = promotion_blockers(state, [])
    assert any("no explicit human disposition" in blocker for blocker in blockers)
    assert any("issue dependent is pending" in blocker for blocker in blockers)


def test_s07_uncertain_dispatch_recovers_once_without_duplicate_launch(tmp_path):
    """G17/CB-D1..D4: one intent, no second process, one audited resolution."""
    store = new_store(tmp_path, "uncertain")
    store.save(RunState(
        run_id="run", slug="feature", phase="ESCALATED", stage="WORK", issues={},
        workers={"w1": {"attempt_id": "worker-w1-2", "status": "uncertain",
                        "uncertain_attempt_id": "worker-w1-2"}}))
    store.event("run", "dispatch_intent", agent="w1", attempt="worker-w1-2")
    store.event("run", "agent_launch_uncertain", agent="w1", attempt="worker-w1-2")

    resolved = resolve_uncertain_dispatch(
        store, "run", "worker-w1-2", operator="operator", reason="audited review")
    events = store.events("run")
    assert resolved["kind"] == "uncertain_attempt_resolved"
    assert len([event for event in events
                if event["kind"] == "uncertain_attempt_resolved"]) == 1
    assert len([event for event in events
                if event["kind"] == "dispatch_intent"]) == 1
    assert not any(event.get("kind") in {"process_started", "process_exited"}
                   for event in events)
    recovered = store.load("run")
    assert recovered.workers["w1"].get("uncertain_attempt_id") is None
    assert recovered.workers["w1"]["status"] == "interrupted"

    revision = store.load("run").revision
    again = resolve_uncertain_dispatch(
        store, "run", "worker-w1-2", operator="other", reason="changed arguments")
    assert again["id"] == resolved["id"]
    assert store.load("run").revision == revision
    assert len([event for event in store.events("run")
                if event["kind"] == "uncertain_attempt_resolved"]) == 1

    # A duplicate resolution event is contradictory and refused.
    store.event("run", "uncertain_attempt_resolved", agent="w1",
                attempt="worker-w1-2", operator="operator",
                reason="second decision", evidence_event_ids=[],
                process_identity_events=0)
    with pytest.raises(ContractError, match="multiple resolution events"):
        resolve_uncertain_dispatch(
            store, "run", "worker-w1-2", operator="operator", reason="audited review")


def test_s07_recovery_decision_replays_after_crash(tmp_path):
    """CB-D4: a crash after the recovery decision reuses it and finishes once."""
    store = new_store(tmp_path, "recovery-crash")
    store.save(RunState(
        run_id="run", slug="feature", phase="ESCALATED", stage="WORK", issues={},
        workers={"w1": {"attempt_id": "worker-w1-2", "status": "uncertain",
                        "uncertain_attempt_id": "worker-w1-2"}}))
    store.event("run", "dispatch_intent", agent="w1", attempt="worker-w1-2")
    store.event("run", "agent_launch_uncertain", agent="w1", attempt="worker-w1-2")

    with crash_at(store, "record_operation",
                  key_prefix="uncertain-recovery:", before=False), pytest.raises(Crash):
        resolve_uncertain_dispatch(
            store, "run", "worker-w1-2", operator="operator",
            reason="audited review")
    interrupted = store.load("run")
    assert any(key.startswith("uncertain-recovery:") for key in interrupted.operations)
    assert interrupted.workers["w1"]["uncertain_attempt_id"] == "worker-w1-2"
    assert not any(event["kind"] == "uncertain_attempt_resolved"
                   for event in store.events("run"))

    event = resolve_uncertain_dispatch(
        store, "run", "worker-w1-2", operator="operator", reason="audited review")
    events = store.events("run")
    assert event["kind"] == "uncertain_attempt_resolved"
    assert len([item for item in events
                if item["kind"] == "uncertain_attempt_resolved"]) == 1
    operation = next(value for key, value in store.load("run").operations.items()
                     if key.startswith("uncertain-recovery:"))
    assert operation["status"] == "completed"


# ------------------------------------------- S08 historical preservation boundary


def test_s08_incomplete_inventory_is_refused_and_history_is_preserved():
    """G23/CH14: explicit migration preserves history or refuses an incomplete inventory."""
    legacy = IssueState(
        id="code-1", worker="w1", root_issue_id="code-1", status="open",
        evidence=["round-1/verdict-code.md"], lifecycle_history=[],
        assignments=[AssignmentState(
            repairs=3, failed_repairs=2, review_rounds=2, assessed_repairs=2,
            failed_repair_ordinals=[1, 2])])
    state = RunState(run_id="legacy-run", slug="feature", phase="ESCALATED",
                     stage="REVIEW", version=1, issues={"code-1": legacy})
    before = state.model_dump(mode="json")

    with pytest.raises(ContractError, match="explicit pending/in_repair mapping"):
        migrate_v1_state(state)
    assert state.model_dump(mode="json") == before
    with pytest.raises(ContractError, match="assignment identity inventory is incomplete"):
        migrate_v1_state(state, issue_lifecycles={"code-1": "in_repair"},
                         assignment_ids={"code-1": []})
    assert state.model_dump(mode="json") == before
    with pytest.raises(ContractError, match="historical"):
        require_operational_schema(state)

    migrated = migrate_v1_state(
        state, issue_lifecycles={"code-1": "in_repair"},
        assignment_ids={"code-1": ["code-1:0"]})
    issue_state = migrated.issues["code-1"]
    assert migrated.version == 2
    assert issue_state.status == "in_repair"
    assert issue_state.legacy_status == "open"
    assert issue_state.evidence == ["round-1/verdict-code.md"]
    assignment = issue_state.assignments[0]
    assert (assignment.repairs, assignment.failed_repairs, assignment.review_rounds,
            assignment.assessed_repairs, assignment.failed_repair_ordinals) == (
        3, 2, 2, 2, [1, 2])
    # The predecessor copy was not rewritten and no allowance was replenished.
    assert state.model_dump(mode="json") == before


# ------------------------------------------- S09 ownership and immutable candidates


def test_s09_ownership_mismatch_refuses_before_mutation(tmp_path):
    """G21/E11: a reviewer may report/resolve only its own issues."""
    store = new_store(tmp_path, "ownership")
    state = seed(store, {
        "docs-1": issue("docs-1", owner="docs"),
        "legacy": IssueState(id="legacy", worker="w1", root_issue_id="legacy",
                             status="in_repair",
                             assignments=[AssignmentState(repairs=1,
                                                          assessed_repairs=0,
                                                          failed_repair_ordinals=[])]),
    })
    before = run_bytes(store)
    with pytest.raises(ContractError, match="owned by docs"):
        accept(store, state,
               Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                       findings=[finding("docs-1")]),
               evidence=EVIDENCE_2, tmp_path=tmp_path)
    assert run_bytes(store) == before

    with pytest.raises(ContractError, match="no validated owner"):
        accept(store, state,
               Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                       findings=[finding("legacy")]),
               evidence=EVIDENCE_3, tmp_path=tmp_path)
    assert run_bytes(store) == before


def test_s09_candidate_binding_is_immutable(tmp_path):
    """G22/E12: each verdict binds the exact candidate; a late/other verdict refuses."""
    store = new_store(tmp_path, "candidate")
    state = seed(store, {"A": issue("A")})
    stale = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                    findings=[finding("A")])
    stale_path = verdict_file(tmp_path, stale)
    before = run_bytes(store)

    with pytest.raises(ContractError, match="not the reviewed candidate"):
        accept(store, state, stale, evidence=EVIDENCE_2, path=stale_path,
               candidate=CANDIDATE_B)
    assert run_bytes(store) == before
    assert not any(key.startswith("review-result:code:" + CANDIDATE_B)
                   for key in store.load("run").operations)

    accepted, result = accept(store, state, stale, evidence=EVIDENCE_2,
                              path=stale_path)
    assert result.candidate_sha == CANDIDATE_A
    # The same verdict cannot close work on a different candidate.
    with pytest.raises(ContractError, match="not the reviewed candidate"):
        accept(store, accepted, stale, evidence=EVIDENCE_3, path=stale_path,
               candidate=CANDIDATE_B, attempt="review-code-2-1")


# ------------------------------------------- S10 end-to-end chain consistency


def test_s10_intact_chain_is_verified_and_effects_match_decisions(tmp_path):
    """G19/CH01-CH13: one consistent chain; applied decisions equal their effects."""
    store = new_store(tmp_path, "chain")
    state = seed(store, {"A": issue("A"), "B": issue("B", status="finished",
                                                     repairs=1, assessed=1)})
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                      findings=[finding("A")], resolved=["B"])
    state, result = accept(store, state, verdict, evidence=EVIDENCE_2,
                           tmp_path=tmp_path)
    application = state.review_applications[
        state.operations[result.transaction]["application_id"]]
    events = store.events("run")

    assessment = require_authoritative_evidence(
        state, application.application_id, operation="acceptance", events=events)
    assert assessment.classification == "verified"
    assert assessment.provenance is not None
    assert assessment.provenance.transaction == result.transaction

    # CH09/CH10: each decision attributes the exact issue/ordinal and its effect.
    for entry in application.entries:
        decision = entry.decision
        assert decision["issue"] == entry.issue_id
        accounting = decision["accounting"]
        assert accounting["issue"] == entry.issue_id
        if accounting["failed_repair_charged"]:
            issue_state = state.issues[entry.issue_id]
            assert accounting["repair_ordinal"] in \
                issue_state.assignments[-1].failed_repair_ordinals
        if accounting["assessment"] == "resolved":
            assert accounting["failed_repair_charged"] is False
            assert state.issues[entry.issue_id].status == "finished"
    # CH12: completion implies every entry applied and every audit agrees.
    assert application.complete is True
    assert application.fully_applied is True
    assert review_transaction_gaps(state, events) == []
    # Promotion is still blocked by the issue the review left in repair, not B.
    blockers = promotion_blockers(state, events)
    assert any("A" in blocker and "in_repair" in blocker for blocker in blockers)
    assert not any(blocker.startswith("issue B") for blocker in blockers)


def test_s10_broken_chain_links_are_classified_not_consumed(verified_application):
    """G19/CH06/CH07/CH11/CH13: break one link at a time; nothing is consumed."""
    store, state, application = verified_application
    events = store.events("run")

    role_broken = state.model_copy(deep=True)
    role_broken.review_applications[application.application_id] = \
        application.model_copy(update={"reviewer_role": "docs"})
    assessment = classify_review_evidence(
        role_broken, application.application_id, operation="acceptance",
        events=events)
    assert assessment.classification == "conflict"

    verdict_broken = state.model_copy(deep=True)
    verdict_broken.review_applications[application.application_id] = \
        application.model_copy(update={"verdict_sha256": "f" * 64})
    assessment = classify_review_evidence(
        verdict_broken, application.application_id, operation="acceptance",
        events=events)
    assert assessment.classification == "conflict"

    completion_broken = state.model_copy(deep=True)
    completion_broken.review_applications[application.application_id] = \
        application.model_copy(update={"complete": False})
    assessment = classify_review_evidence(
        completion_broken, application.application_id, operation="acceptance",
        events=events)
    assert assessment.classification == "unverifiable"
    assert "final completion record" in assessment.missing_inputs

    early_complete = state.model_copy(deep=True)
    broken = application.model_copy(deep=True)
    broken.entries[0] = broken.entries[0].model_copy(
        update={"state": "not_applied", "decision": None})
    early_complete.review_applications[application.application_id] = \
        broken.model_copy(update={"complete": True})
    assessment = classify_review_evidence(
        early_complete, application.application_id, operation="acceptance",
        events=events)
    assert assessment.classification == "conflict"


def test_s10_accounting_tamper_on_interrupted_replay_is_refused(tmp_path):
    """CH09/CH13: replay compares its durable decision; a changed ordinal refuses."""
    store = new_store(tmp_path, "tamper")
    state = seed(store, {"A": issue("A")})
    verdict = Verdict(reviewer="code", result="rework", candidate_sha=CANDIDATE_A,
                      findings=[finding("A")])
    path = verdict_file(tmp_path, verdict)
    with crash_at(store, "complete_review_application", call=1), \
            pytest.raises(Crash):
        accept(store, state, verdict, evidence=EVIDENCE_2, path=path)

    interrupted = store.load("run")
    operation = next(value for key, value in interrupted.operations.items()
                     if key.startswith("review-result:"))
    operation["accounting_decisions"][0]["repair_ordinal"] += 10
    before = run_bytes(store)
    with pytest.raises(ContractError, match="accounting decision disagrees"):
        resume_review_applications(store, interrupted, manifest())
    assert run_bytes(store) == before


def test_s10_dispatch_and_completion_provenance_are_mandatory():
    """CH03/CH04/CH05: missing dispatch/completion/ownership evidence is unverifiable."""
    base = ReviewerProvenance(
        reviewer_role="code", attempt_id="review-code-1-1",
        candidate_sha=CANDIDATE_A, verdict_sha256="f" * 64,
        evidence_digest=EVIDENCE_2, verdict_path="/tmp/verdict-code.md",
        transaction="review-result:code:x:y", ownership_valid=True,
        dispatch_evidence=True, completion_evidence=True)
    for field in ("ownership_valid", "dispatch_evidence", "completion_evidence"):
        broken = base.model_copy(update={field: False})
        with pytest.raises(UnverifiableEvidenceError):
            assess_repair_outcome(issue("A"), "A", broken)


# ------------------------- S11 combined lifecycle x product (models CLI) coverage


def make_model_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "app.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    return repo


def test_s11_approval_binds_model_preferences_and_carry_forward_inventory(
        tmp_path, plan_factory):
    """S1: effective preferences and the approved inventory are pinned; shadowed edits are inert."""
    plan, manifest = plan_factory(slug="prefs")
    inventory = plan / "inventory.md"
    inventory.write_text("# Carry-forward inventory\n\n- committed interface\n")
    manifest = manifest.model_copy(update={"carry_forward_inventory": "inventory.md"})

    feature = FeatureSpec(
        slug="prefs", target_project=tmp_path, requirement=plan / "requirement.md",
        models=RoleModels(worker=ModelSpec(model="feature/coder")))
    write_markdown(plan / "feature.md", feature)
    config = Config(models=RoleModels(
        worker=ModelSpec(model="global/coder", reasoning="low")))
    effective = effective_feature(feature, config, plan / "feature.md")
    assert effective.models.worker.model == "feature/coder"
    assert effective.models.worker.reasoning == "low"

    approved = approval_hashes(plan, manifest, effective)
    assert str(inventory.resolve()) in approved
    assert detect_approval_changes(approved, plan, manifest, effective) == []

    # A global edit fully shadowed by the explicit feature model changes nothing.
    shadowed = effective_feature(
        feature,
        Config(models=RoleModels(worker=ModelSpec(model="global/other", reasoning="low"))),
        plan / "feature.md")
    assert shadowed.models.worker.model == "feature/coder"
    assert detect_approval_changes(approved, plan, manifest, shadowed) == []

    changed = feature.model_copy(update={"models": RoleModels(
        worker=ModelSpec(model="feature/coder", reasoning="high"))})
    changed_effective = effective_feature(changed, config, plan / "feature.md")
    changes = detect_approval_changes(approved, plan, manifest, changed_effective)
    assert any("effective_configuration: content changed" in item for item in changes)

    # The optional approved inventory is bound exactly like other reviewed inputs.
    inventory.write_text("# Carry-forward inventory\n\n- changed input\n")
    changes = detect_approval_changes(approved, plan, manifest, effective)
    assert any(str(inventory.resolve()) in item and "content changed" in item
               for item in changes)


def test_s11_guarded_lifecycle_invalidates_approval_after_model_write(
        tmp_path, plan_factory, monkeypatch):
    """S2: a repository preference write changes boundary settings and stops dispatch."""
    plan, manifest = plan_factory(slug="prefs")
    repo = make_model_repo(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    write_markdown(plan / "feature.md", FeatureSpec(
        slug="prefs", target_project=repo, requirement=plan / "requirement.md"))

    def resolve():
        current, _ = read_markdown(plan / "feature.md", FeatureSpec)
        resolution = resolve_config(repo)
        return effective_feature(current, resolution.config, plan / "feature.md"), \
            resolution.config

    assert run_models(parser().parse_args([
        "models", "set", "worker", "--model", "global/coder", "--reasoning", "low"])) == 0
    feature, _ = resolve()
    assert feature.models.worker.model == "global/coder"

    store = StateStore(tmp_path / "state")
    state = store.save(RunState(
        run_id="prefs-run", slug="prefs", phase="RUNNING", stage="WORK",
        approved_hashes=approval_hashes(plan, manifest, feature),
        workspace={"repo": str(repo), "feature_path": str(tmp_path / "checkout"),
                   "feature_branch": "feature/prefs", "base_sha": "0" * 40,
                   "workers": {}}))
    engine = Engine(store, state, feature, manifest, plan, adapters={},
                    settings_loader=resolve)
    engine.check_approval()
    assert state.phase == "RUNNING"

    assert run_models(parser().parse_args([
        "models", "set", "worker", "--model", "repo/coder", "--reasoning", "high",
        "--scope", "repo", "--target", str(repo)])) == 0
    written = (repo / ".orch.toml").read_text()
    assert 'model = "repo/coder"' in written
    assert 'reasoning = "high"' in written

    with pytest.raises(ApprovalRequired):
        engine.check_approval()
    assert state.phase == "PLAN_READY"
    events = store.events("prefs-run")
    assert "approval_invalidated" in [event["kind"] for event in events]
    assert not any(event["kind"] in {"dispatch_intent", "process_started"}
                   for event in events)
    # The in-flight invocation keeps the settings it resolved; nothing is hot-swapped.
    assert engine.feature.models.worker.model == "global/coder"


def test_s11_approval_diagnostics_are_sanitized_for_feature_and_inventory(tmp_path):
    """S1 sanitization on approval inputs: paths/types only, never values or unknown keys."""
    sentinel = "https://hooks.invalid/approval-secret"
    plan = tmp_path / "plan"
    plan.mkdir()

    feature = plan / "feature.md"
    feature.write_text(
        "---\nslug: demo\ntarget_project: /tmp/x\nrequirement: requirement.md\n"
        f"credential-secret: {sentinel}\n---\n")
    with pytest.raises(ContractError) as caught:
        read_markdown(feature, FeatureSpec)
    message = str(caught.value)
    assert sentinel not in message
    assert "credential-secret" not in message
    assert "<unrecognized field>: extra_forbidden" in message
    assert str(feature) in message

    manifest_path = plan / "manifest.md"
    manifest_path.write_text(
        "---\nslug: demo\nbranch: feature/demo\nworkers:\n  - id: w1\n"
        "    title: work\n    instruction: workers/w1.md\n    allowed_paths: [app/**]\n"
        f'carry_forward_inventory: "../{sentinel}.md"\n---\n')
    with pytest.raises(ContractError) as caught:
        read_markdown(manifest_path, Manifest)
    message = str(caught.value)
    assert sentinel not in message
    assert "carry_forward_inventory: value_error" in message
    assert str(manifest_path) in message

    duplicate = plan / "duplicate.md"
    duplicate.write_text(f"---\nslug: demo\n{sentinel}: a\n{sentinel}: b\n---\n")
    with pytest.raises(ContractError, match="Duplicate or non-string YAML key"):
        read_markdown(duplicate, FeatureSpec)


def test_s11_model_commands_leave_dependency_and_promotion_gates_unchanged(
        tmp_path, monkeypatch):
    """S2/S3: models show/set never load or mutate run state, scheduling or gates."""
    plan = tmp_path / "plan"
    plan.mkdir()
    record = DependencyRecord(
        plan_version="v1", source_verdicts=["review-result:code:x:y"],
        dependencies=[
            FindingDependency(dependent_issue_id="dependent",
                              prerequisite_issue_ids=["independent"],
                              rationale="needs the committed interface"),
            FindingDependency(dependent_issue_id="shared-a",
                              shared_resource_paths=["app/shared.py"],
                              rationale="same file"),
            FindingDependency(dependent_issue_id="shared-b",
                              shared_resource_paths=["app/shared.py"],
                              rationale="same file"),
        ])
    write_markdown(plan / "dependencies.md", record, "## Dependencies\n")
    store = new_store(tmp_path, "models-gates")
    state = seed(store, {
        "independent": issue("independent", status="pending", repairs=0, assessed=None),
        "dependent": issue("dependent", status="pending", repairs=0, assessed=None),
        "shared-a": issue("shared-a", status="pending", repairs=0, assessed=None),
        "shared-b": issue("shared-b", status="pending", repairs=0, assessed=None),
        "escalated": issue("escalated", status="escalated", repairs=2, failed=1,
                           assessed=2, ordinals=[1]),
    })
    dependencies = load_validated_dependencies(dependency_manifest(plan), plan, state)
    candidates = [state.issues[identity] for identity in
                  ("independent", "dependent", "shared-a", "shared-b")]
    selected, waiting = select_dispatchable_issues(candidates, state, dependencies)
    before_blockers = promotion_blockers(state, store.events("run"))
    before = run_bytes(store)

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    assert run_models(parser().parse_args([
        "models", "set", "worker", "--model", "provider/worker",
        "--reasoning", "low"])) == 0
    assert run_models(parser().parse_args(["models", "show"])) == 0

    assert run_bytes(store) == before
    reloaded = store.load("run")
    after_selected, after_waiting = select_dispatchable_issues(
        [reloaded.issues[identity] for identity in
         ("independent", "dependent", "shared-a", "shared-b")],
        reloaded, dependencies)
    assert [item.id for item in after_selected] == [item.id for item in selected]
    assert after_waiting == waiting
    assert promotion_blockers(reloaded, store.events("run")) == before_blockers

