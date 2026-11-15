"""Deterministic stage scheduling, approval gates and per-issue repair accounting."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import portalocker

from orch.carry_forward import (
    CARRY_FORWARD_IMPORT_OPERATION,
    load_inventory,
)
from orch.config import Config
from orch.contracts import (
    ActivityRecord,
    ApprovalRequiredError,
    AssignmentState,
    CheckError,
    ContractError,
    Discussion,
    EvidenceConflictError,
    FeatureSpec,
    Finding,
    Handover,
    HumanDisposition,
    IssueState,
    LifecycleTransition,
    LifecycleTransitionError,
    Manifest,
    RepairOutcomeCharge,
    ReviewerProvenance,
    ReviewResult,
    RunState,
    TransitionGuard,
    Verdict,
    apply_human_disposition,
    apply_lifecycle_transition,
    approval_hashes,
    approved_files,
    assess_repair_outcome,
    build_review_application,
    classify_review_evidence,
    create_pending_issue,
    create_system_issue,
    finish_system_issue,
    load_dependency_record,
    read_markdown,
    validate_dependency_record,
    within,
)
from orch.reporting import write_report
from orch.store import StateStore, atomic_write, issues_markdown, timestamp


class ApprovalRequired(ApprovalRequiredError):
    pass


class Escalation(RuntimeError):
    pass


def _exception_leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in _exception_leaves(child)]
    return [error]


def _exception_details(error: BaseException) -> list[str]:
    """Return leaf failure messages instead of TaskGroup's generic wrapper text."""
    if isinstance(error, BaseExceptionGroup):
        return [message for child in error.exceptions for message in _exception_details(child)]
    if isinstance(error, asyncio.CancelledError):
        return []
    return [str(error) or type(error).__name__]


def _failure_message(error: BaseException) -> str:
    details = list(dict.fromkeys(_exception_details(error)))
    if not details:
        return "Operation interrupted"
    if len(details) == 1:
        return details[0]
    return "Concurrent failures: " + "; ".join(details)


def _preflight_failure(error: BaseException):
    """Return a prelaunch rejection only when every leaf proves one.

    A mixed or unknown group keeps the fail-closed uncertain-launch path even
    if one member happened to fail validation.
    """
    from orch.agents import PreflightError

    leaves = _exception_leaves(error)
    if leaves and all(isinstance(leaf, PreflightError) for leaf in leaves):
        return leaves[0]
    return None


_INVOCATION_FIELDS = ("logical_id", "attempt_id", "adapter", "provider", "model")


def _identity_text(value) -> str:
    if not isinstance(value, str):
        return "unknown"
    text = "".join(c for c in value if ord(c) >= 32 and ord(c) != 127).strip()
    return text or "unknown"


def record_invocation_identity(info: dict, logical_id: str, attempt_id: str,
                               event: dict) -> dict | None:
    """Persist one attempt's adapter invocation evidence, never preferences.

    The event must name the exact active logical agent and attempt. A repeated
    identical event is idempotent; conflicting evidence is retained with an
    explicit conflicting status so presentation shows unknown instead of
    silently overwriting history.
    """
    if not isinstance(info, dict) or not isinstance(attempt_id, str):
        return None
    if event.get("logical_id") != logical_id or event.get("attempt_id") != attempt_id:
        return None
    mapping = info.get("invocation_attempts")
    if mapping is None:
        mapping = {}
        info["invocation_attempts"] = mapping
    if not isinstance(mapping, dict):
        return None
    entry = {
        "logical_id": logical_id,
        "attempt_id": attempt_id,
        "adapter": _identity_text(event.get("adapter")),
        "provider": _identity_text(event.get("provider")),
        "model": _identity_text(event.get("model")),
        "evidence_status": "recorded",
    }
    existing = mapping.get(attempt_id)
    if existing is None:
        mapping[attempt_id] = entry
    elif not isinstance(existing, dict):
        mapping[attempt_id] = {**entry, "evidence_status": "conflicting"}
    elif all(existing.get(field) == entry[field] for field in _INVOCATION_FIELDS):
        return existing
    else:
        mapping[attempt_id] = {**entry, "evidence_status": "conflicting"}
    return mapping[attempt_id]


_ATTEMPT_TERMINAL_EVENTS = frozenset({
    "agent_completed",
    "agent_failed",
    "agent_interrupted",
    "dispatch_preflight_failed",
    "dispatch_blocked",
})
_PROCESS_EVENT_KINDS = frozenset({
    "process_started",
    "process_children",
    "process_completed",
})
# The workflow journals agent_interruption_uncertain for a cancellation before
# process identity. agent_interrupted_uncertain is accepted only as the legacy
# spelling already recorded by the first repair candidate; it is never emitted.
_UNCERTAINTY_EVENTS = frozenset({
    "agent_launch_uncertain",
    "agent_interruption_uncertain",
    "agent_interrupted_uncertain",
})
_RECOVERY_OPERATION_PREFIX = "uncertain-recovery:"
_HUMAN_DISPOSITION_OPERATION_PREFIX = "human-disposition:"


def _dispatch_activity(issue: IssueState, worker_id: str, attempt: int) -> ActivityRecord:
    """Real dispatch activity, kept separate from repair counters."""
    return ActivityRecord(
        activity_id=f"dispatch:{issue.id}:{attempt}",
        kind="dispatch", issue_id=issue.id,
        assignment_id=(issue.assignments[-1].assignment_id
                       or f"{issue.id}:{len(issue.assignments) - 1}"),
        attempt=attempt, process_ownership="known", started_at=timestamp(),
        evidence=[f"worker:{worker_id}"],
    )


def _change_snapshot(root: Path, paths: list[str]) -> str:
    """Hash changed worktree content independently of staged/committed diff form."""
    digest = hashlib.sha256()
    for relative in sorted(paths):
        path = root / relative
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif path.is_file():
            digest.update(b"file\0")
            digest.update(b"x" if path.stat().st_mode & 0o111 else b"-")
            digest.update(path.read_bytes())
        else:
            digest.update(b"missing\0")
    return digest.hexdigest()


def validate_plan(plan: Path) -> Manifest:
    manifest, _ = read_markdown(plan / "manifest.md", Manifest)
    discussion, _ = read_markdown(plan / "discussion.md", Discussion)
    if discussion.blocking_questions or not discussion.ready:
        raise ContractError("Planning discussion is not ready or has blocking questions")
    for path in approved_files(plan, manifest):
        if not path.is_file():
            raise ContractError(f"Missing plan artifact: {path}")
    if "## Interfaces" not in (plan / "plan.md").read_text(encoding="utf-8"):
        raise ContractError("plan.md must include an Interfaces section")
    return manifest


def next_action(state: RunState) -> str:
    if state.phase in {"DRAFT", "DISCUSSING"}:
        return "PLAN"
    if state.phase == "PLAN_READY":
        return "APPROVE"
    if state.phase in {"APPROVED", "RUNNING"}:
        return state.stage
    return {"PAUSED": "RESUME", "ESCALATED": "RESUME", "AWAITING_TEST": "HUMAN_TEST",
            "DONE": "NONE", "ABORTED": "NONE"}[state.phase]


_REVIEW_ROLES = ("code", "docs")
_REVIEW_RESULT_PREFIX = "review-result:"
_REVIEW_APPLICATION_PREFIX = "review-application:"
_REVIEW_BATCH_RECOVERY_PREFIX = "review-batch-recovery:"
_LEGACY_REVIEW_CONFLICT = "Issue is simultaneously resolved and open"
_SYSTEM_ISSUE_TEMPLATES = ("check-{}", "post-check-{}", "integration-check-{}")
# Lifecycle values that may still receive an authorized dispatch.
_DISPATCHABLE = frozenset({"pending", "in_repair"})
# Named dispatch refusal while the approved carry-forward import is incomplete.
CARRY_FORWARD_IMPORT_PENDING = "carry_forward_import_pending"
# A finding must reopen or continue work; these are the only reviewer-owned
# lifecycle effects. Escalation is issue-scoped and never truncates the plan.
_FINDING_STAYS = frozenset({"pending", "in_repair"})


def system_issue_identity(identity: str, manifest: Manifest) -> bool:
    """Return whether the workflow itself owns this exact generated issue ID."""
    return any(identity == template.format(worker.id)
               for worker in manifest.workers for template in _SYSTEM_ISSUE_TEMPLATES)


@dataclass
class ReviewRoleOutcome:
    role: str
    result: ReviewResult | None = None
    error: str | None = None
    reused: bool = False


@dataclass
class RoleEntryPlan:
    """One ordered, deterministic entry of a complete role routing plan."""

    entry_id: str
    order_index: int
    kind: str
    issue_id: str
    issue: IssueState
    decision: dict


@dataclass
class RoleRoutingPlan:
    """A pure, fully validated routing result for one reviewer role.

    Nothing in the caller's issue register is mutated while this plan is built.
    Every finding and resolution produces exactly one ordered entry. An
    issue-scoped stop is recorded on that entry (and in ``escalations``); it
    never truncates the remaining entries.
    """

    issues: dict[str, IssueState]
    findings: dict[str, Finding]
    resolved: list[str]
    touched: list[str]
    affected_workers: list[str]
    accounting_decisions: list[dict]
    entries: list[RoleEntryPlan]
    escalations: list[str]
    # Retained compatibility view: the first issue-scoped stop, if any.
    escalation: str | None = None


def review_application_id(transaction: str) -> str:
    """Stable application identity derived from a role transaction."""
    if transaction.startswith(_REVIEW_RESULT_PREFIX):
        return transaction.replace("review-result", "review-application", 1)
    return f"{_REVIEW_APPLICATION_PREFIX}{transaction}"


def _charge_dict(charge: RepairOutcomeCharge) -> dict:
    """The retained accounting decision shape used by role transactions."""
    return {
        "issue": charge.issue,
        "assignment_index": charge.assignment_index,
        "repair_ordinal": charge.repair_ordinal,
        "assessment": charge.assessment,
        "failed_repair_charged": charge.failed_repair_charged,
        "review_round_charged": charge.review_round_charged,
    }


def _lifecycle_source(issue: IssueState) -> str:
    """Canonical source status, refusing ambiguous version-one records."""
    return issue.current_lifecycle()


def _apply_planned_lifecycle(source: IssueState, plan_issue: IssueState,
                             guard: TransitionGuard, *, evidence: list[str]) -> IssueState:
    """Replay one planned lifecycle effect through the guarded contract.

    ``plan_issue`` already carries the planned counters and accounting outcome;
    only the status transition is validated here, from the exact pre-application
    source. This is the reducer's only status write path.
    """
    working = plan_issue.model_copy(deep=True)
    working.status = _lifecycle_source(source)
    apply_lifecycle_transition(working, plan_issue.status, guard, evidence=evidence)
    return working


def _planned_lifecycle(source: IssueState, target: str) -> tuple[str, TransitionGuard]:
    """Return the canonical source and guard fact for a planned status effect."""
    current = _lifecycle_source(source)
    if target == current:
        return current, TransitionGuard(no_change_evidence=True)
    guard = TransitionGuard()
    if (current, target) == ("pending", "in_repair") or (current, target) == ("in_repair", "in_repair"):
        guard.subsequent_attempt = True
    elif (current, target) == ("in_repair", "finished"):
        guard.reviewer_acceptance = True
    elif (current, target) == ("finished", "pending"):
        guard.reopening_valid = True
        guard.historical_attribution = True
        guard.remaining_work = True
    elif (current, target) == ("finished", "escalated"):
        guard.reopening_valid = True
        guard.stop_boundary = True
    elif (current, target) == ("pending", "escalated") or (current, target) == ("in_repair", "escalated"):
        guard.explicit_escalation = True
    elif (current, target) == ("escalated", "finished"):
        guard.scoped_human_acceptance = True
        guard.owner_disposition = True
        guard.acceptance_evidence = True
    elif (current, target) == ("pending", "finished"):
        # Only an owning reviewer acceptance reaches this point, and the plan
        # does not manufacture a human disposition for a pending issue.
        raise LifecycleTransitionError(
            f"Issue {source.id}: a pending issue cannot be finished by reviewer "
            "acceptance alone; dispatch it or record an explicit human disposition"
        )
    else:
        raise LifecycleTransitionError(
            f"Issue {source.id}: lifecycle effect {current} -> {target} is not available "
            "to a review application"
        )
    return current, guard


def _validate_repair_counters(identity: str, assignment: AssignmentState) -> None:
    """Refuse internally contradictory repair counters before any routing."""
    if assignment.failed_repairs > assignment.repairs:
        raise ContractError(
            f"Issue {identity} records {assignment.failed_repairs} failed repairs for "
            f"{assignment.repairs} repair attempt(s). Counters are inconsistent, so the "
            "assessment history cannot be trusted. Refusing to reset or reinterpret them; "
            "audit the retained review transactions and use an approved recovery."
        )
    charged = assignment.failed_repair_ordinals
    if (charged is not None
            and (charged != sorted(set(charged))
                 or any(ordinal <= 0 or ordinal > assignment.repairs for ordinal in charged)
                 or len(charged) != assignment.failed_repairs)):
        raise ContractError(
            f"Issue {identity} has contradictory failed-repair ordinal provenance; "
            "the exact charge history cannot be trusted. Refusing to continue."
        )
    mark = assignment.assessed_repairs
    if mark is not None:
        if mark > assignment.repairs:
            raise ContractError(
                f"Issue {identity} records assessment of repair {mark} but only "
                f"{assignment.repairs} repair attempt(s). Explicit provenance is "
                "inconsistent with the retained counters; refusing to continue."
            )
        if assignment.failed_repairs > mark:
            raise ContractError(
                f"Issue {identity} records {assignment.failed_repairs} failed repair(s) but "
                f"only repair outcome {mark} as assessed. Explicit provenance is "
                "inconsistent with the retained counters; refusing to continue."
            )
        if assignment.failed_repair_ordinals and max(assignment.failed_repair_ordinals) > mark:
            raise ContractError(
                f"Issue {identity} records a failed repair ordinal above assessed outcome {mark}; "
                "explicit provenance is inconsistent with the retained counters. Refusing to continue."
            )


def repair_assessment_evidence(
    state: RunState,
) -> dict[tuple[str, int], dict[int, dict[str, list]]]:
    """Derive per-assignment repair assessments from completed review transactions.

    A completed role transaction records the complete pre-routing register and the
    accepted verdict. When that verdict reports or resolves an issue, the base
    assignment's repair ordinal is the outcome that review assessed. The mapping
    is keyed by issue, assignment position and repair ordinal, and retains the
    transaction identities as audit evidence.

    Only completed transactions are evidence. An accepted role result without a
    completed transaction makes the history incomplete and is refused rather than
    filled in from counters.
    """
    evidence: dict[tuple[str, int], dict[int, dict[str, list]]] = {}
    completed: set[str] = set()
    charged: dict[tuple[str, int], set[int]] = {}
    for key, operation in state.operations.items():
        if not key.startswith(_REVIEW_RESULT_PREFIX):
            continue
        if not isinstance(operation, dict) or operation.get("status") != "completed":
            continue
        completed.add(key)
        base = operation.get("base_issues")
        recorded = operation.get("verdict")
        if not isinstance(base, dict) or not isinstance(recorded, dict):
            raise ContractError(
                f"Review transaction {key} has no retained pre-routing register or verdict; "
                "repair-assessment provenance cannot be established"
            )
        try:
            verdict = Verdict.model_validate(recorded)
        except (ValueError, TypeError) as exc:
            raise ContractError(
                f"Review transaction {key} has a malformed retained verdict: {exc}"
            ) from exc
        finding_ids = {finding.id for finding in verdict.findings}
        findings = {finding.id for finding in verdict.findings}
        decisions = operation.get("accounting_decisions")
        if decisions is not None and not isinstance(decisions, list):
            raise ContractError(f"Review transaction {key} has malformed accounting provenance")
        decisions_by_issue = {}
        if decisions is not None:
            for decision in decisions:
                if not isinstance(decision, dict) or not isinstance(decision.get("issue"), str):
                    raise ContractError(
                        f"Review transaction {key} has malformed accounting provenance"
                    )
                identity = decision["issue"]
                if identity in decisions_by_issue:
                    raise ContractError(
                        f"Review transaction {key} duplicates accounting provenance for {identity}"
                    )
                decisions_by_issue[identity] = decision
        for identity in finding_ids | set(verdict.resolved):
            base_issue = base.get(identity)
            if not isinstance(base_issue, dict):
                continue
            try:
                issue = IssueState.model_validate(base_issue)
            except (ValueError, TypeError) as exc:
                raise ContractError(
                    f"Review transaction {key} has a malformed retained register for "
                    f"{identity}: {exc}"
                ) from exc
            if not issue.assignments:
                continue
            assignment_index = len(issue.assignments) - 1
            ordinal = issue.assignments[-1].repairs
            assignment_key = (identity, assignment_index)
            assignment = issue.assignments[-1]
            known_charges = charged.setdefault(assignment_key, set())
            if assignment.failed_repair_ordinals is not None:
                known_charges.update(assignment.failed_repair_ordinals)
            elif (assignment.assessed_repairs is not None
                  and assignment.failed_repairs == assignment.assessed_repairs):
                known_charges.update(range(1, assignment.failed_repairs + 1))
            record = evidence.setdefault((identity, assignment_index), {}).setdefault(
                ordinal, {"findings": [], "resolved": [], "charges": [],
                          "pending_legacy_charges": [], "decisions": []}
            )
            is_finding = identity in findings
            record["findings" if is_finding else "resolved"].append(key)
            decision = decisions_by_issue.get(identity) if decisions is not None else None
            if decisions is not None:
                if decision is None:
                    raise ContractError(
                        f"Review transaction {key} has no accounting decision for {identity}"
                    )
                expected_assignment = decision.get("assignment_index")
                expected_ordinal = decision.get("repair_ordinal")
                assessment = decision.get("assessment")
                charged_ordinals = set(issue.assignments[-1].failed_repair_ordinals or [])
                semantically_valid = (
                    (assessment == "initial_work" and is_finding
                     and ordinal == 0 and not decision.get("failed_repair_charged")
                     and not decision.get("review_round_charged"))
                    or (assessment == "new_failure" and is_finding and ordinal > 0
                        and ordinal not in charged_ordinals
                        and ((issue.assignments[-1].assessed_repairs is not None
                              and ordinal > issue.assignments[-1].assessed_repairs)
                             or (issue.assignments[-1].assessed_repairs is None
                                 and issue.assignments[-1].failed_repairs == 0
                                 and issue.assignments[-1].review_rounds == 0
                                 and issue.status not in ("resolved", "finished")))
                        and decision.get("failed_repair_charged") is True
                        and decision.get("review_round_charged") is True)
                    or (assessment == "superseding_failure" and is_finding and ordinal > 0
                        and issue.status in ("resolved", "finished")
                        and issue.assignments[-1].assessed_repairs == ordinal
                        and ordinal not in charged_ordinals
                        and decision.get("failed_repair_charged") is True
                        and not decision.get("review_round_charged"))
                    or (assessment == "already_charged" and is_finding
                        and ordinal in charged_ordinals
                        and not decision.get("failed_repair_charged")
                        and not decision.get("review_round_charged"))
                    or (assessment == "already_assessed" and is_finding
                        and issue.assignments[-1].assessed_repairs is not None
                        and ordinal <= issue.assignments[-1].assessed_repairs
                        and not decision.get("failed_repair_charged")
                        and not decision.get("review_round_charged"))
                    or (assessment == "resolved" and not is_finding
                        and not decision.get("failed_repair_charged")
                        and decision.get("review_round_charged") is (
                            issue.status in ("open", "pending", "in_repair")
                            and (issue.assignments[-1].assessed_repairs is None
                                 or ordinal > issue.assignments[-1].assessed_repairs)))
                )
                if (expected_assignment != assignment_index or expected_ordinal != ordinal
                        or assessment not in {
                            "initial_work", "new_failure", "superseding_failure",
                            "already_charged", "already_assessed", "resolved",
                        }
                        or not isinstance(decision.get("failed_repair_charged"), bool)
                        or not isinstance(decision.get("review_round_charged"), bool)
                        or not semantically_valid):
                    raise ContractError(
                        f"Review transaction {key} has contradictory accounting provenance for "
                        f"{identity}"
                    )
            else:
                # Schema-v1 completed transactions predate explicit decisions.
                # Reconstruct only what their retained pre-routing snapshot proves.
                already_charged = ordinal in known_charges
                mark = assignment.assessed_repairs
                if not is_finding:
                    assessment = "resolved"
                elif ordinal == 0:
                    assessment = "initial_work"
                elif already_charged or (assignment.failed_repair_ordinals is not None
                                         and ordinal in assignment.failed_repair_ordinals):
                    assessment = "already_charged"
                elif issue.status in ("resolved", "finished") and mark == ordinal:
                    if not record["resolved"]:
                        raise ContractError(
                            f"Review transaction {key} reopens {identity} at repair {ordinal} "
                            "but no retained successful assessment identifies the superseded "
                            "outcome; refusing ambiguous legacy attribution"
                        )
                    assessment = "superseding_failure"
                    record["pending_legacy_charges"].append(key)
                elif ((mark is None and assignment.repairs > 0
                       and assignment.failed_repairs == 0 and assignment.review_rounds == 0
                       and issue.status not in ("resolved", "finished"))
                      or (mark is not None and ordinal > mark)):
                    assessment = "new_failure"
                else:
                    assessment = "already_assessed"
                decision = {
                    "issue": identity, "assignment_index": assignment_index,
                    "repair_ordinal": ordinal, "assessment": assessment,
                    "failed_repair_charged": assessment in {
                        "new_failure", "superseding_failure"},
                    "review_round_charged": assessment == "new_failure",
                    "legacy_inferred": True,
                }
            record["decisions"].append({"transaction": key, **decision})
            if decision["failed_repair_charged"]:
                record["charges"].append(key)
                charged.setdefault((identity, assignment_index), set()).add(ordinal)
            elif is_finding and decision["assessment"] == "already_charged" \
                    and ordinal in known_charges:
                # The current transaction did not add a charge, but its retained
                # base snapshot independently preserves the exact earlier charge.
                record["charges"].append(key)
    missing = sorted(set(state.review_results) - completed)
    if missing:
        raise ContractError(
            "Accepted role results have no completed review transaction: "
            + ", ".join(missing)
            + "; repair-assessment provenance cannot be established"
        )
    return evidence


def _failed_repair_ordinals(records: dict[int, dict[str, list]]) -> set[int]:
    """Return repair ordinals assessed as unsuccessful actual repairs.

    Ordinal zero is initial work before any repair has been dispatched. A
    finding there is valid assessment provenance and establishes a mark of zero,
    but it never consumes a failed-repair allowance because no repair happened.
    """
    return {ordinal for ordinal, record in records.items()
            if ordinal > 0 and (record.get("charges") if "charges" in record
                                else record.get("findings"))}


def _derive_assessed_repairs(identity: str, assignment: AssignmentState,
                             records: dict[int, dict[str, list]]) -> int:
    """Derive an assessment mark from completed-review evidence or refuse."""
    _validate_repair_counters(identity, assignment)
    if not records:
        if assignment.failed_repairs or assignment.review_rounds:
            raise ContractError(
                f"Issue {identity} has failed or reviewed repair counters but no completed "
                "review transaction evidence. Counters alone cannot prove which repair "
                "outcome was assessed. Refusing to guess; audit the retained verdicts and "
                "use an approved recovery instead."
            )
        # An open issue with no failure charge, no review round and no completed
        # review evidence has not had a repair outcome assessed yet.
        return 0
    failure_ordinals = _failed_repair_ordinals(records)
    pending = {ordinal for ordinal, record in records.items()
               if record.get("pending_legacy_charges")}
    if assignment.failed_repairs > len(failure_ordinals):
        raise ContractError(
            f"Issue {identity} records {assignment.failed_repairs} failed repair(s) but "
            f"completed reviews show only {len(failure_ordinals)} distinct unsuccessful "
            "repair outcome(s). The counters may contain a duplicate charge from an "
            "earlier revalidation. Refusing to guess or reset them; audit the retained "
            "review transactions and use an approved recovery."
        )
    if assignment.failed_repairs < len(failure_ordinals):
        missing = len(failure_ordinals) - assignment.failed_repairs
        if missing == len(pending) and pending and assignment.failed_repair_ordinals is None:
            # Old routing accepted a same-outcome reopening but did not charge
            # it. The pre-routing resolved snapshot and exact assignment/ordinal
            # prove the superseding failure; materialize it once in the next
            # accepted transaction's base snapshot.
            assignment.failed_repairs += missing
            assignment.failed_repair_ordinals = sorted(failure_ordinals)
        else:
            raise ContractError(
                f"Issue {identity} records {assignment.failed_repairs} failed repair(s) but "
                f"retained reviews show {len(failure_ordinals)} distinct unsuccessful repair "
                "outcome(s). The counters understate the assessed history; refusing to continue."
            )
    if assignment.failed_repair_ordinals is None:
        assignment.failed_repair_ordinals = sorted(failure_ordinals)
    elif set(assignment.failed_repair_ordinals) != failure_ordinals:
        raise ContractError(
            f"Issue {identity} records failed-repair ordinals "
            f"{assignment.failed_repair_ordinals} but completed review decisions show "
            f"{sorted(failure_ordinals)}; refusing to continue."
        )
    assessed = max(records)
    if assessed > assignment.repairs:
        raise ContractError(
            f"Issue {identity} has review evidence for repair {assessed} but only "
            f"{assignment.repairs} repair attempt(s). Retained evidence and counters "
            "disagree; refusing to continue."
        )
    return assessed


def derive_repair_assessment(
    issues: dict[str, IssueState],
    evidence: dict[tuple[str, int], dict[int, dict[str, list]]],
    finding_ids: set[str],
    relevant_ids: set[str] | None = None,
) -> dict[str, IssueState]:
    """Return a copy with legacy finding assessments derived from evidence.

    Explicit marks are validated against the counters and, when retained review
    evidence exists, against the exact assessed repair outcome. Resolution
    targets are not derived here: the current passing verdict is itself their
    assessment. They are still checked for contradictory explicit counters.
    """
    work = {identity: issue.model_copy(deep=True) for identity, issue in issues.items()}
    for identity in (relevant_ids if relevant_ids is not None else finding_ids):
        issue = work.get(identity)
        if issue is None or not issue.assignments:
            continue
        for index, assignment in enumerate(issue.assignments):
            records = evidence.get((identity, index), {})
            if not records:
                _validate_repair_counters(identity, assignment)
                if assignment.assessed_repairs is None and (
                        assignment.failed_repairs or assignment.review_rounds):
                    raise ContractError(
                        f"Issue {identity} assignment {index} has failure or review counters "
                        "but no completed review transaction evidence. Refusing to guess."
                    )
                if assignment.failed_repair_ordinals is None:
                    if assignment.failed_repairs == 0:
                        assignment.failed_repair_ordinals = []
                    elif assignment.failed_repairs == assignment.assessed_repairs:
                        # With every assessed positive ordinal charged, the exact
                        # set is unambiguous even for an older explicit mark.
                        assignment.failed_repair_ordinals = list(
                            range(1, assignment.failed_repairs + 1))
                    else:
                        raise ContractError(
                            f"Issue {identity} assignment {index} has failed-repair counters "
                            "without enough retained evidence to identify the charged ordinals. "
                            "Refusing to guess."
                        )
                continue
            if (assignment.assessed_repairs is not None
                    and assignment.assessed_repairs != max(records)):
                raise ContractError(
                    f"Issue {identity} records repair outcome {assignment.assessed_repairs} "
                    f"as assessed but retained reviews assessed outcome {max(records)}. "
                    "Explicit provenance disagrees with the retained evidence; refusing to continue."
                )
            derived = _derive_assessed_repairs(identity, assignment, records)
            if index == len(issue.assignments) - 1 and identity in finding_ids:
                if assignment.assessed_repairs is None:
                    assignment.assessed_repairs = derived
            elif assignment.assessed_repairs is not None and assignment.assessed_repairs != max(records):
                raise ContractError(
                    f"Issue {identity} records repair outcome {assignment.assessed_repairs} "
                    f"as assessed but retained reviews assessed outcome {max(records)}. "
                    "Explicit provenance disagrees with the retained evidence; refusing to continue."
                )
            elif assignment.assessed_repairs is None:
                assignment.assessed_repairs = derived
    return work


def _assess_repair_outcome(issue: IssueState, identity: str,
                           provenance: ReviewerProvenance | None = None,
                           *, resolved: bool = False,
                           assignment_index: int | None = None) -> dict:
    """Apply and describe the accounting effect of this finding.

    ``repairs`` counts worker dispatch attempts for the issue. ``assessed_repairs``
    records the highest repair ordinal whose outcome a reviewer has already
    judged, so each actual repair is charged at most once even when a changed
    approval or a fresh reviewer run routes the same retained feedback again.

    A missing mark is only safe for an open issue with no failure or review
    history; all other legacy histories must be normalised from completed review
    evidence by the caller first. Counters alone never prove an assessment.

    The caller owns a deep copy, so mutating the counters here cannot affect the
    accepted register unless the whole plan validates.

    When reviewer provenance is supplied (the authoritative acceptance path) the
    committed W01 accounting authority owns the decision. The workflow-local
    reconstruction below is retained only for pure planning of legacy registers
    whose provenance has not been assembled yet.
    """
    if provenance is not None:
        # A role review assesses the current owning assignment's latest repair;
        # an explicitly supplied historical index is honoured for recovery.
        index = (assignment_index if assignment_index is not None
                 else len(issue.assignments) - 1)
        retained = None
        if index is not None and index < len(issue.assignments):
            assignment = issue.assignments[index]
            ordinal = assignment.repairs
            expected = ("successful" if resolved
                        else "initial_work" if ordinal == 0 else "unsuccessful")
            # The W01 authority stores one outcome per assignment ordinal. A
            # valid reopening supersedes an earlier successful assessment, and a
            # later pass may revalidate an earlier unsuccessful repair. Either
            # way the differing earlier assessment is retained as history, so
            # charge on a normalized copy and restore it afterwards rather than
            # falsifying it.
            retained = [
                outcome for outcome in assignment.outcomes
                if outcome.repair_ordinal == ordinal and outcome.outcome != expected
            ]
        if retained:
            working = issue.model_copy(deep=True)
            current = working.assignments[index]
            current.outcomes = [
                outcome for outcome in current.outcomes
                if not (outcome.repair_ordinal == current.repairs
                        and outcome.outcome != expected)
            ]
            charge = assess_repair_outcome(
                working, identity, provenance, assignment_index=index,
                resolved=resolved)
            working.assignments[index].outcomes = (
                list(working.assignments[index].outcomes) + retained)
            issue.assignments = working.assignments
            return _charge_dict(charge)
        charge = assess_repair_outcome(
            issue, identity, provenance, assignment_index=index,
            resolved=resolved)
        return _charge_dict(charge)
    if resolved:
        # Pure-planning projection of a passing assessment. The authoritative
        # path above delegates the same decision to the W01 authority.
        assignment_index = len(issue.assignments) - 1
        current = issue.assignments[-1]
        _validate_repair_counters(identity, current)
        was_active = issue.lifecycle in _DISPATCHABLE
        new_outcome = (current.assessed_repairs is None
                       or current.repairs > current.assessed_repairs)
        round_charged = was_active and new_outcome
        if round_charged:
            current.review_rounds += 1
        if current.assessed_repairs is None:
            current.assessed_repairs = current.repairs
        else:
            current.assessed_repairs = max(current.assessed_repairs, current.repairs)
        if current.failed_repair_ordinals is None:
            current.failed_repair_ordinals = []
        return {
            "issue": identity, "assignment_index": assignment_index,
            "repair_ordinal": current.repairs, "assessment": "resolved",
            "failed_repair_charged": False, "review_round_charged": round_charged,
        }
    assignment_index = len(issue.assignments) - 1
    current = issue.assignments[-1]
    _validate_repair_counters(identity, current)
    if current.failed_repair_ordinals is None:
        if current.repairs > 0 and current.assessed_repairs is None and (
                current.failed_repairs or current.review_rounds
                or issue.status in ("resolved", "finished")):
            raise ContractError(
                f"Issue {identity} has repair history without durable assessment provenance. "
                "Counters alone cannot prove which repair outcome was assessed."
            )
        if current.failed_repairs == current.assessed_repairs:
            current.failed_repair_ordinals = list(range(1, current.failed_repairs + 1))
        elif current.failed_repairs == 0:
            current.failed_repair_ordinals = []
        else:
            raise ContractError(
                f"Issue {identity} has failed repairs but no exact charged-ordinal provenance. "
                "Derive it from completed review transactions before routing."
            )
    if current.repairs == 0:
        if current.assessed_repairs is None:
            current.assessed_repairs = 0
        return {"issue": identity, "assignment_index": assignment_index,
                "repair_ordinal": 0, "assessment": "initial_work",
                "failed_repair_charged": False, "review_round_charged": False}
    if current.assessed_repairs is None:
        if current.failed_repairs or current.review_rounds \
                or issue.status in ("resolved", "finished"):
            raise ContractError(
                f"Issue {identity} has repair history without durable assessment "
                "provenance. Counters alone cannot prove which repair outcome was "
                "assessed. Refusing to guess; derive the mark from completed review "
                "transactions or use an approved recovery."
            )
        current.assessed_repairs = current.repairs
        current.failed_repairs += 1
        current.failed_repair_ordinals.append(current.repairs)
        current.review_rounds += 1
        return {"issue": identity, "assignment_index": assignment_index,
                "repair_ordinal": current.repairs, "assessment": "new_failure",
                "failed_repair_charged": True, "review_round_charged": True}
    ordinal = current.repairs
    if ordinal in current.failed_repair_ordinals:
        return {"issue": identity, "assignment_index": assignment_index,
                "repair_ordinal": ordinal, "assessment": "already_charged",
                "failed_repair_charged": False, "review_round_charged": False}
    if ordinal > current.assessed_repairs:
        current.assessed_repairs = current.repairs
        current.failed_repairs += 1
        current.failed_repair_ordinals.append(ordinal)
        current.review_rounds += 1
        return {"issue": identity, "assignment_index": assignment_index,
                "repair_ordinal": ordinal, "assessment": "new_failure",
                "failed_repair_charged": True, "review_round_charged": True}
    if issue.status in ("resolved", "finished") and ordinal == current.assessed_repairs:
        current.failed_repairs += 1
        current.failed_repair_ordinals.append(ordinal)
        return {"issue": identity, "assignment_index": assignment_index,
                "repair_ordinal": ordinal, "assessment": "superseding_failure",
                "failed_repair_charged": True, "review_round_charged": False}
    return {"issue": identity, "assignment_index": assignment_index,
            "repair_ordinal": ordinal, "assessment": "already_assessed",
            "failed_repair_charged": False, "review_round_charged": False}


def plan_role_routing(issues: dict[str, IssueState], verdict: Verdict,
                      manifest: Manifest, role: str, *,
                      provenance: ReviewerProvenance | None = None) -> RoleRoutingPlan:
    """Validate one role's verdict and build its complete ordered plan.

    Ownership, lineage, within-role pass completeness and budgets are checked
    here. Nothing in the caller's register is mutated.

    Every finding and resolution produces one entry. An issue-scoped stop
    (explicit escalation, depth rule, allowance exhaustion, reassignment
    refusal) is recorded on that entry and in ``escalations``; it never returns
    early or drops the remaining entries. Missing parents or a lineage cycle are
    a whole-verdict defect and are refused before any entry is planned.

    When ``provenance`` is supplied the committed W01 accounting authority owns
    each repair decision. Without it this remains a pure planning helper for a
    legacy register whose provenance has not yet been assembled.
    """
    if role not in _REVIEW_ROLES:
        raise ContractError(f"Unknown reviewer role: {role!r}")
    if verdict.reviewer != role:
        raise ContractError(f"Verdict identity {verdict.reviewer!r} does not match role {role!r}")
    workers = {w.id for w in manifest.workers}
    work = {identity: issue.model_copy(deep=True) for identity, issue in issues.items()}
    # This is a pure planning projection, not an authoritative migration: a
    # version-one ``open`` record is planned as pending, or in_repair when it
    # already records repair activity. Committing an ordinary operation still
    # refuses an ambiguous base register, and an explicit migration remains the
    # only route to authoritative version-two state.
    for issue in work.values():
        if issue.status == "resolved":
            issue.status = "finished"
        elif issue.status == "open":
            issue.status = "in_repair" if issue.has_repair_activity else "pending"
    findings: dict[str, Finding] = {}
    for finding in verdict.findings:
        if finding.worker not in workers or not set(finding.related_workers) <= workers:
            raise ContractError(f"Unknown finding owner: {finding.worker}")
        existing = work.get(finding.id)
        if existing is not None:
            if existing.owner_role is None:
                raise ContractError(
                    f"Issue {finding.id} has no validated owner; resolve legacy ownership first"
                )
            if existing.owner_role != role:
                raise ContractError(
                    f"{role} reviewer may not report {finding.id}; it is owned by {existing.owner_role}"
                )
        if finding.id in findings and findings[finding.id] != finding:
            raise ContractError(f"Reviewers disagree on identity of {finding.id}")
        findings[finding.id] = finding

    resolved = list(dict.fromkeys(verdict.resolved))
    for identity in resolved:
        issue = work.get(identity)
        if issue is None:
            raise ContractError(f"Unknown resolved issue: {identity}")
        if issue.owner_role is None:
            raise ContractError(
                f"Issue {identity} has no validated owner; resolve legacy ownership first"
            )
        if issue.owner_role != role:
            raise ContractError(
                f"{role} reviewer may not resolve {identity}; it is owned by {issue.owner_role}"
            )
    if set(resolved) & set(findings):
        raise ContractError("Issue is simultaneously resolved and open")
    if verdict.result == "pass":
        missing = sorted(issue.id for issue in work.values()
                         if issue.owner_role == role
                         and issue.lifecycle in _DISPATCHABLE
                         and issue.id not in resolved)
        if missing:
            raise ContractError("Passing verdict must explicitly resolve " + ", ".join(missing))

    touched: set[str] = set()
    accounting_decisions: list[dict] = []
    entries: list[RoleEntryPlan] = []
    escalations: list[str] = []

    remaining = dict(findings)
    while remaining:
        progress = False
        for identity, finding in list(remaining.items()):
            previous = work.get(identity)
            if finding.parent_issue_id and finding.parent_issue_id not in work:
                continue
            parent = work.get(finding.parent_issue_id)
            depth = parent.depth + 1 if parent else 0
            root = parent.root_issue_id if parent else identity
            if finding.root_issue_id != root:
                raise ContractError(f"Incorrect root for {identity}")
            if previous is not None:
                if (previous.parent_issue_id, previous.worker) != (
                        finding.parent_issue_id, finding.worker):
                    raise ContractError(f"Issue identity changed: {identity}")
                before_lifecycle = previous.lifecycle
                decision = _assess_repair_outcome(
                    previous, identity, provenance)
                accounting_decisions.append(decision)
            else:
                previous = create_pending_issue(
                    identity, finding.worker, root_issue_id=root,
                    parent_issue_id=finding.parent_issue_id, depth=depth,
                    owner_role=role, summary=finding.summary,
                )
                work[identity] = previous
                before_lifecycle = previous.lifecycle
                decision = {
                    "issue": identity, "assignment_index": 0, "repair_ordinal": 0,
                    "assessment": "initial_work", "failed_repair_charged": False,
                    "review_round_charged": False,
                }
                accounting_decisions.append(decision)
            touched.add(identity)
            stopped: str | None = None
            reassigned = False

            if depth > manifest.limits.max_issue_depth:
                stopped = (f"Derived issue {identity} exceeds depth limit; "
                           f"escalate root {root}")
                target = work[root]
                if target.lifecycle in _DISPATCHABLE or target.lifecycle == "finished":
                    target.status = "escalated"
                touched.add(root)
                escalations.append(stopped)

            if finding.action == "escalate" and stopped is None:
                stopped = f"Issue {identity} requires escalation"
                if previous.lifecycle in _DISPATCHABLE or previous.lifecycle == "finished":
                    previous.status = "escalated"
                escalations.append(stopped)

            if stopped is None:
                current = previous.assignments[-1]
                new_failure_charge = decision["failed_repair_charged"]
                exhausted = (
                    (new_failure_charge
                     and current.failed_repairs >= manifest.limits.max_failed_repairs_per_assignment)
                    or (new_failure_charge
                        and current.repairs >= manifest.limits.max_repair_attempts)
                    or (decision["review_round_charged"]
                        and current.review_rounds >= manifest.limits.max_review_rounds)
                )
                if exhausted or finding.action == "reassign":
                    if len(previous.assignments) - 1 >= manifest.limits.max_reassignments:
                        stopped = f"Issue {identity} exhausted replacement allowance"
                        if previous.lifecycle in _DISPATCHABLE:
                            previous.status = "escalated"
                        escalations.append(stopped)
                    else:
                        previous.assignments.append(
                            AssignmentState(failed_repair_ordinals=[]))
                        reassigned = True

            if stopped is None:
                if previous.lifecycle == "finished":
                    # A valid owning-reviewer finding reopens finished work.
                    previous.status = "pending"
                elif previous.lifecycle == "escalated":
                    # A repeated ordinary finding cannot silently release an
                    # escalated issue. It is issue-scoped and consumes nothing.
                    stopped = (f"Issue {identity} is escalated; ordinary review "
                               "cannot reopen it without explicit human continuation")
                    escalations.append(stopped)

            entry_id = f"finding:{identity}"
            entry_decision = {
                "kind": "finding", "issue": identity,
                "lifecycle": {"from": before_lifecycle, "to": previous.lifecycle},
                "accounting": dict(decision),
                "reopened": before_lifecycle == "finished" and previous.lifecycle == "pending",
                "reassigned": reassigned,
                "escalation": stopped,
            }
            entries.append(RoleEntryPlan(
                entry_id=entry_id, order_index=len(entries), kind="finding",
                issue_id=identity, issue=previous.model_copy(deep=True),
                decision=entry_decision,
            ))
            del remaining[identity]
            progress = True
        if not progress:
            raise ContractError("Issue lineage has missing parents or a cycle")

    for identity in resolved:
        issue = work[identity]
        before_lifecycle = issue.lifecycle
        _validate_repair_counters(identity, issue.assignments[-1])
        decision = _assess_repair_outcome(
            issue, identity, provenance, resolved=True)
        accounting_decisions.append(decision)
        stopped: str | None = None
        if before_lifecycle == "escalated":
            stopped = (f"Issue {identity} is escalated; an ordinary resolution "
                       "cannot silently release it without explicit human continuation")
            escalations.append(stopped)
        elif before_lifecycle == "finished":
            # Re-resolving a finished issue is a validated no-change entry.
            pass
        elif before_lifecycle == "in_repair":
            issue.status = "finished"
        elif before_lifecycle == "pending":
            raise ContractError(
                f"Issue {identity} is pending, not in repair; a reviewer cannot finish "
                "undispatched work. Dispatch it or record an explicit human disposition."
            )
        touched.add(identity)
        entry_id = f"resolution:{identity}"
        entries.append(RoleEntryPlan(
            entry_id=entry_id, order_index=len(entries), kind="resolution",
            issue_id=identity, issue=issue.model_copy(deep=True),
            decision={
                "kind": "resolution", "issue": identity,
                "lifecycle": {"from": before_lifecycle, "to": issue.lifecycle},
                "accounting": dict(decision),
                "reopened": False, "reassigned": False,
                "escalation": stopped,
            },
        ))

    return RoleRoutingPlan(
        issues=work, findings=findings, resolved=resolved, touched=sorted(touched),
        affected_workers=sorted({f.worker for f in findings.values()}),
        accounting_decisions=accounting_decisions, entries=entries,
        escalations=escalations,
        escalation=escalations[0] if escalations else None,
    )


def _worker_ancestors(manifest: Manifest, worker_id: str) -> set[str]:
    by_id = {worker.id: worker for worker in manifest.workers}
    seen: set[str] = set()
    stack = list(by_id[worker_id].depends_on)
    while stack:
        dependency = stack.pop()
        if dependency in seen or dependency not in by_id:
            continue
        seen.add(dependency)
        stack.extend(by_id[dependency].depends_on)
    return seen


def _dependency_chain(manifest: Manifest, root: str, worker_id: str) -> list[str] | None:
    """Return one root-to-worker dependency path through dependents."""
    by_id = {worker.id: worker for worker in manifest.workers}
    queue: list[list[str]] = [[root]]
    while queue:
        path = queue.pop(0)
        node = path[-1]
        if node == worker_id:
            return path
        for candidate in by_id.values():
            if node in candidate.depends_on and candidate.id not in path:
                queue.append([*path, candidate.id])
    return None


def _verdict_findings(result) -> list[Finding]:
    try:
        verdict, _ = read_markdown(Path(result.verdict_path), Verdict)
    except (ContractError, OSError, TypeError):
        return []
    return list(verdict.findings)


def build_rework_feedback(worker_id: str, manifest: Manifest, affected: set[str],
                          results: list) -> str:
    """Render useful evidence for a directly reviewed or dependency-expanded worker.

    A worker invalidated only because a dependency was reworked never appears in
    any role's ``affected_workers``. It still needs the upstream findings, the
    verdict paths and the dependency chain that invalidated its checked result;
    an empty feedback body would waste a repair attempt.
    """
    by_id = {worker.id: worker for worker in manifest.workers}
    ancestors = _worker_ancestors(manifest, worker_id)
    resolved = [(result, _verdict_findings(result)) for result in results]
    own = [(result, finding) for result, findings in resolved
           for finding in findings if finding.worker == worker_id]
    upstream = [(result, finding) for result, findings in resolved
                for finding in findings if finding.worker in ancestors]
    direct_dependencies = sorted(dependency for dependency in by_id[worker_id].depends_on
                                 if dependency in affected)

    lines = ["# Review feedback", ""]
    if own:
        lines.extend(["## Findings for your work", ""])
        for result, finding in own:
            lines.append(f"- {finding.id} ({finding.severity}): {finding.summary}")
            lines.append(f"  - evidence: {result.verdict_path}")
    if direct_dependencies:
        lines.extend([
            "## Dependency rework",
            "",
            ("Your previous CHECK and review evidence no longer covers the updated "
             f"dependency {', '.join(direct_dependencies)} that review returned for rework. "
             "Revalidate your work after the dependency changes are available; do not treat "
             "the earlier result as current."),
        ])
        roots = sorted({finding.worker for _result, finding in upstream
                        if finding.worker in affected})
        chains: list[list[str]] = []
        for root in roots:
            chain = _dependency_chain(manifest, root, worker_id)
            if chain and chain not in chains:
                chains.append(chain)
        if chains:
            lines.extend(["", "Dependency chain(s):"])
            lines.extend(f"- {' -> '.join(chain)}" for chain in chains)
        if upstream:
            lines.extend(["", "Upstream findings:"])
            for result, finding in upstream:
                lines.append(
                    f"- {finding.id} ({finding.severity}) for {finding.worker}: "
                    f"{finding.summary}"
                )
                lines.append(f"  - evidence: {result.verdict_path}")
        elif not own:
            lines.extend(["", "Upstream review evidence:"])
            lines.extend(f"- Read {result.verdict_path}" for result, _findings in resolved)
    if not own and not direct_dependencies:
        lines.extend([
            "## Upstream review evidence",
            "",
            ("Your checked result was invalidated by review rework. Read the verdict evidence "
             "before continuing:"),
        ])
        lines.extend(f"- Read {result.verdict_path}" for result, _findings in resolved)
    if len(lines) <= 2:
        lines.extend([
            "## Upstream review evidence",
            "",
            ("Review requested rework that affects this work. Preserve the existing worktree and "
             "revalidate before continuing."),
        ])
        lines.extend(f"- Read {result.verdict_path}" for result, _findings in resolved)
    return "\n".join(lines) + "\n"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _issues_digest(issues: dict[str, IssueState]) -> str:
    payload = {identity: issue.model_dump(mode="json") for identity, issue in issues.items()}
    return _sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def review_operation_key(role: str, candidate_sha: str, evidence_digest: str) -> str:
    return f"{_REVIEW_RESULT_PREFIX}{role}:{candidate_sha}:{evidence_digest}"


def _review_acceptance_events(events: list[dict], key: str) -> list[dict]:
    return [event for event in events
            if event.get("kind") == "review_result_accepted"
            and event.get("transaction") == key]


def _operation_provenance(key: str, operation: dict) -> ReviewerProvenance:
    """Assemble the mandatory reviewer provenance for one accepted operation."""
    try:
        return ReviewerProvenance(
            reviewer_role=operation["role"],
            attempt_id=str(operation["attempt_id"]),
            candidate_sha=str(operation["candidate_sha"]),
            verdict_sha256=str(operation["verdict_sha256"]),
            evidence_digest=str(operation["evidence_digest"]),
            verdict_path=str(operation["verdict_path"]),
            transaction=key,
            ownership_valid=True,
            dispatch_evidence=True,
            completion_evidence=True,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(
            f"Review transaction {key} has incomplete reviewer provenance: {exc}"
        ) from exc


def _entry_issue(entry: RoleEntryPlan, base: IssueState | None, *,
                 transaction: str) -> IssueState:
    """Replay one entry's planned status effect through the guarded contract."""
    if base is None:
        source = entry.issue.model_copy(deep=True)
        source.status = "pending"
        source.lifecycle_history = []
    else:
        source = base.model_copy(deep=True)
    final = entry.issue.model_copy(deep=True)
    if final.status == source.lifecycle:
        current = _lifecycle_source(source)
        guard = TransitionGuard(no_change_evidence=True)
    else:
        current, guard = _planned_lifecycle(source, final.status)
    working = final.model_copy(deep=True)
    working.status = current
    apply_lifecycle_transition(
        working, final.lifecycle, guard,
        evidence=[f"transaction:{transaction}", f"entry:{entry.entry_id}"],
    )
    return working


def _ensure_review_application(store: StateStore, state: RunState,
                               application) -> RunState:
    """Persist a new intent or verify the already-durable one.

    An interrupted application retains applied entry state, so it is no longer
    byte-identical to a freshly built intent. Replay compares only the stable
    intent fields and never rewrites applied decisions.
    """
    existing = state.review_applications.get(application.application_id)
    if existing is None:
        return store.begin_review_application(state, application)
    stable = (
        existing.transaction, existing.reviewer_role, existing.attempt_id,
        existing.candidate_sha, existing.evidence_digest, existing.verdict_sha256,
        existing.verdict_path, existing.approved_inputs,
    )
    expected = (
        application.transaction, application.reviewer_role, application.attempt_id,
        application.candidate_sha, application.evidence_digest,
        application.verdict_sha256, application.verdict_path,
        application.approved_inputs,
    )
    if stable != expected:
        raise EvidenceConflictError(
            f"Review application {application.application_id} already has a "
            "different durable intent"
        )
    stored_entries = {entry.entry_id: entry.proposed_decision
                      for entry in existing.entries}
    built_entries = {entry.entry_id: entry.proposed_decision
                     for entry in application.entries}
    if stored_entries != built_entries:
        raise EvidenceConflictError(
            f"Review application {application.application_id} durable entry intent "
            "disagrees with the replayed decision"
        )
    return state


def _apply_planned_entries(store: StateStore, state: RunState, application_id: str,
                           plan: RoleRoutingPlan,
                           base_issues: dict[str, IssueState]) -> RunState:
    """Apply every not-yet-applied planned entry in deterministic order.

    An issue-scoped escalation does not stop the loop: later entries are still
    validated and committed. Application never launches a worker.
    """
    for entry_plan in plan.entries:
        application = state.review_applications[application_id]
        stored = next(item for item in application.entries
                      if item.entry_id == entry_plan.entry_id)
        if stored.state == "applied":
            if stored.decision != entry_plan.decision:
                raise EvidenceConflictError(
                    f"Entry {entry_plan.entry_id} was already applied with a different decision"
                )
        else:
            issue = _entry_issue(
                entry_plan, base_issues.get(entry_plan.issue_id),
                transaction=application.transaction,
            )
            state = store.apply_review_entry(
                state, application_id, entry_plan.entry_id,
                decision=entry_plan.decision, issue=issue,
            )
        state = store.record_review_entry_audit(
            state, application_id, entry_plan.entry_id)
    return store.complete_review_application(state, application_id)


def _apply_unescorted_side_effects(store: StateStore, state: RunState,
                                   plan: RoleRoutingPlan,
                                   entry_issue_ids: set[str]) -> RunState:
    """Apply planned lifecycle effects that are not themselves review entries.

    A depth-rule stop names the affected root, which may not be a finding or
    resolution in the same verdict. It is still a guarded transition, recorded
    after the entries are applied; it never drops or replaces an entry.
    """
    for identity in plan.touched:
        if identity in entry_issue_ids:
            continue
        source = state.issues.get(identity)
        target = plan.issues.get(identity)
        if source is None or target is None or source.lifecycle == target.lifecycle:
            continue
        current, guard = _planned_lifecycle(source, target.lifecycle)
        updated = target.model_copy(deep=True)
        updated.status = current
        transition = apply_lifecycle_transition(
            updated, target.lifecycle, guard,
            evidence=["review-application:lineage-stop"],
        )
        state.issues[identity] = updated
        state = store.save(state)
        store.event(
            state.run_id, "lifecycle_transition", issue=identity,
            from_status=transition.from_status, to_status=transition.to_status,
            guard=transition.guard, evidence=transition.evidence,
            reason="issue-scoped lineage stop",
        )
    return state


def _build_application_issues(snapshot_issues: dict[str, IssueState],
                              plan: RoleRoutingPlan) -> dict[str, IssueState]:
    """Return intent-time snapshots plus synthetic pending new issues.

    Snapshots must be the exact register the intent was persisted against. A
    normalized planning copy is not enough: the store refuses an entry whose
    pre-application evidence does not match the durable register.
    """
    view = dict(snapshot_issues)
    for entry in plan.entries:
        if entry.issue_id in view:
            continue
        synthetic = entry.issue.model_copy(deep=True)
        synthetic.status = "pending"
        synthetic.lifecycle_history = []
        view[entry.issue_id] = synthetic
    return view


def _recorded_application_issues(operation: dict,
                                 fallback: dict[str, IssueState]
                                 ) -> dict[str, IssueState]:
    recorded = operation.get("application_base_issues")
    if not isinstance(recorded, dict):
        return dict(fallback)
    return {identity: IssueState.model_validate(value)
            for identity, value in recorded.items()}


def _finish_review_operation(store: StateStore, state: RunState, key: str,
                             operation: dict, manifest: Manifest) -> tuple[RunState, ReviewResult]:
    """Commit or replay one role acceptance from its recorded durable decision.

    The intent contains the complete pre-routing issue register and the exact
    normalized verdict. Replay recomputes the same plan from that base instead
    of from whatever the live register now says, so a crash between intent,
    issue commit, audit event and completion cannot double-charge a counter.
    """
    if not isinstance(operation, dict):
        raise ContractError(f"Review transaction {key} is malformed")
    status = operation.get("status")
    if status not in {"intent", "completed"}:
        raise ContractError(f"Review transaction {key} has unknown status {status!r}")
    role = operation.get("role")
    candidate_sha = operation.get("candidate_sha")
    evidence_digest = operation.get("evidence_digest")
    if role not in _REVIEW_ROLES or not isinstance(candidate_sha, str) or not isinstance(evidence_digest, str):
        raise ContractError(f"Review transaction {key} has incomplete identity")
    recorded = operation.get("verdict")
    if not isinstance(recorded, dict):
        raise ContractError(f"Review transaction {key} has no recorded verdict")
    verdict = Verdict.model_validate(recorded)
    if verdict.reviewer != role:
        raise ContractError(f"Review transaction {key} records the wrong reviewer identity")
    if status == "completed":
        result = state.review_results.get(key)
        if result is None:
            raise ContractError(f"Completed review transaction {key} has no persisted role result")
        if result.verdict_sha256 != operation.get("verdict_sha256"):
            raise ContractError(f"Completed review transaction {key} has a conflicting verdict digest")
        return state, result

    base = operation.get("base_issues")
    if not isinstance(base, dict):
        raise ContractError(f"Review transaction {key} has no pre-routing issue snapshot")
    base_issues = {identity: IssueState.model_validate(value) for identity, value in base.items()}
    provenance = _operation_provenance(key, operation)
    plan = plan_role_routing(base_issues, verdict, manifest, role, provenance=provenance)
    recorded_decisions = operation.get("accounting_decisions")
    if not isinstance(recorded_decisions, list):
        raise ContractError(
            f"Review transaction {key} has no durable accounting decision; "
            "refusing to reinterpret an interrupted legacy transaction"
        )
    if recorded_decisions != plan.accounting_decisions:
        raise ContractError(
            f"Review transaction {key} accounting decision disagrees with its recorded "
            "pre-routing register; refusing replay"
        )
    reconciliations = operation.get("legacy_reconciliations", [])
    if not isinstance(reconciliations, list):
        raise ContractError(f"Review transaction {key} has malformed legacy reconciliation")
    seen_reconciliations = set()
    for item in reconciliations:
        if (not isinstance(item, dict) or not isinstance(item.get("issue"), str)
                or type(item.get("assignment_index")) is not int
                or type(item.get("repair_ordinal")) is not int
                or not isinstance(item.get("source_transactions"), list)
                or not item["source_transactions"]
                or not all(isinstance(source, str) for source in item["source_transactions"])):
            raise ContractError(f"Review transaction {key} has malformed legacy reconciliation")
        identity = item["issue"]
        index = item["assignment_index"]
        ordinal = item["repair_ordinal"]
        marker = (identity, index, ordinal)
        if marker in seen_reconciliations or ordinal <= 0:
            raise ContractError(f"Review transaction {key} has duplicate legacy reconciliation")
        seen_reconciliations.add(marker)
        issue = base_issues.get(identity)
        if (issue is None or index >= len(issue.assignments)
                or ordinal not in (issue.assignments[index].failed_repair_ordinals or [])):
            raise ContractError(
                f"Review transaction {key} reconciliation does not match its base register"
            )
        for source in item["source_transactions"]:
            source_operation = state.operations.get(source)
            source_base = (source_operation.get("base_issues")
                           if isinstance(source_operation, dict) else None)
            source_issue_raw = source_base.get(identity) if isinstance(source_base, dict) else None
            try:
                source_issue = IssueState.model_validate(source_issue_raw)
                source_verdict = Verdict.model_validate(source_operation.get("verdict"))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ContractError(
                    f"Review transaction {key} legacy source {source} is malformed"
                ) from exc
            if (source_operation.get("status") != "completed"
                    or source_operation.get("accounting_decisions") is not None
                    or source_issue.status not in ("resolved", "finished")
                    or not source_issue.assignments
                    or source_issue.assignments[-1].repairs != ordinal
                    or source_issue.assignments[-1].assessed_repairs != ordinal
                    or identity not in {finding.id for finding in source_verdict.findings}):
                raise ContractError(
                    f"Review transaction {key} legacy source {source} does not prove the "
                    "recorded reopening charge"
                )
    result = ReviewResult(
        role=role,
        candidate_sha=candidate_sha,
        evidence_digest=evidence_digest,
        attempt_id=str(operation.get("attempt_id")),
        verdict_sha256=str(operation.get("verdict_sha256")),
        verdict_path=str(operation.get("verdict_path")),
        result=verdict.result,
        findings=sorted(plan.findings),
        resolved=list(plan.resolved),
        affected_workers=list(plan.affected_workers),
        binding=operation.get("binding") if operation.get("binding") in {"candidate-sha", "legacy-attempt"}
        else "candidate-sha",
        accepted_at=str(operation.get("recorded_at")),
        transaction=key,
        escalation=plan.escalation,
    )
    application_id: str | None = None
    if plan.entries:
        application_issues = _build_application_issues(
            _recorded_application_issues(operation, state.issues), plan)
        application = build_review_application(
            result, verdict, application_issues,
            approved_inputs=operation.get("approved_inputs") or {},
            proposed_decisions={entry.entry_id: entry.decision for entry in plan.entries},
        )
        application_id = application.application_id
        state = _ensure_review_application(store, state, application)
        state = _apply_planned_entries(
            store, state, application_id, plan, application_issues)
        state = _apply_unescorted_side_effects(
            store, state, plan,
            {entry.issue_id for entry in plan.entries},
        )
    else:
        state = store.save(state)

    state.review_results[key] = result
    store.event(
        state.run_id, "review_application_recorded",
        transaction=key, application_id=application_id,
        entry_ids=[entry.entry_id for entry in plan.entries],
        escalations=plan.escalations,
    )
    state = store.save(state)

    events = store.events(state.run_id)
    matching = _review_acceptance_events(events, key)
    if len(matching) > 1:
        raise ContractError(f"Review transaction {key} has multiple acceptance events")
    if matching:
        event = matching[0]
        if (event.get("accounting_decisions") != plan.accounting_decisions
                or event.get("legacy_reconciliations") != reconciliations):
            raise ContractError(
                f"Review transaction {key} acceptance audit disagrees with its accounting decision"
            )
    else:
        event = store.event(
            state.run_id, "review_result_accepted", transaction=key, role=role,
            candidate_sha=candidate_sha, evidence_digest=evidence_digest,
            attempt_id=result.attempt_id, verdict_sha256=result.verdict_sha256,
            verdict_path=result.verdict_path, result=result.result,
            findings=result.findings, resolved=result.resolved,
            affected_workers=result.affected_workers, escalation=result.escalation,
            binding=result.binding, accounting_decisions=plan.accounting_decisions,
            legacy_reconciliations=reconciliations,
            application_id=application_id,
            entry_ids=[entry.entry_id for entry in plan.entries],
        )
    state = store.record_operation(state, key, {
        "status": "completed", "event_id": event["id"],
        "after_issues_digest": _issues_digest(state.issues),
        "completed_at": timestamp(),
        "application_id": application_id,
        "entry_ids": [entry.entry_id for entry in plan.entries],
        "escalations": plan.escalations,
    })
    return state, state.review_results[key]


def accept_review_result(store: StateStore, state: RunState, *, role: str,
                         candidate_sha: str, evidence_digest: str, attempt_id: str,
                         verdict: Verdict, verdict_path: Path, manifest: Manifest,
                         require_candidate_binding: bool = True) -> tuple[RunState, ReviewResult]:
    """Validate and transactionally accept exactly one reviewer role result.

    Validation is complete before a durable intent exists. A conflicting
    verdict for evidence already decided is refused rather than applied; an
    identical replay returns the recorded result without charging counters.
    """
    if role not in _REVIEW_ROLES:
        raise ContractError(f"Unknown reviewer role: {role!r}")
    if verdict.reviewer != role:
        raise ContractError(f"Verdict identity {verdict.reviewer!r} does not match role {role!r}")
    if verdict.candidate_sha is not None and verdict.candidate_sha != candidate_sha:
        raise ContractError(
            f"{role} verdict names candidate {verdict.candidate_sha}, "
            f"not the reviewed candidate {candidate_sha}"
        )
    if require_candidate_binding and verdict.candidate_sha != candidate_sha:
        raise ContractError(
            f"{role} verdict must name the reviewed candidate; include candidate_sha: {candidate_sha}"
        )
    if verdict.result == "escalate":
        raise ContractError("An escalate verdict is not an accepted role result")
    path = Path(verdict_path)
    if not path.is_file():
        raise ContractError(f"Verdict evidence is missing: {path}")
    digest = _file_sha256(path)
    key = review_operation_key(role, candidate_sha, evidence_digest)
    operation = state.operations.get(key)
    if operation is not None:
        if not isinstance(operation, dict):
            raise ContractError(f"Review transaction {key} is malformed")
        if operation.get("verdict_sha256") != digest:
            raise ContractError(
                f"{role} role produced conflicting verdicts for unchanged candidate/evidence; "
                "refusing to charge or apply the later feedback"
            )
        return _finish_review_operation(store, state, key, operation, manifest)

    # Derive legacy assessment marks from completed review evidence and validate
    # explicit marks before the first durable write for this role result.
    finding_ids = {finding.id for finding in verdict.findings}
    relevant_ids = finding_ids | set(verdict.resolved)
    evidence = repair_assessment_evidence(state)
    base_issues = derive_repair_assessment(
        state.issues, evidence, finding_ids, relevant_ids)
    # Refuse an ambiguous legacy lifecycle before the first durable write. A
    # version-one open record with repair activity requires explicit migration;
    # it is never guessed as pending or in repair during acceptance.
    for identity in finding_ids | set(verdict.resolved):
        planned = base_issues.get(identity)
        if planned is not None:
            _lifecycle_source(planned)
    for identity in finding_ids:
        issue = base_issues.get(identity)
        if issue is None or issue.status not in ("resolved", "finished") or not issue.assignments:
            continue
        assignment_index = len(issue.assignments) - 1
        assignment = issue.assignments[-1]
        ordinal = assignment.repairs
        charged_ordinals = set(assignment.failed_repair_ordinals or [])
        if (ordinal > 0 and assignment.assessed_repairs == ordinal
                and ordinal not in charged_ordinals
                and not evidence.get((identity, assignment_index), {}).get(ordinal, {}).get("resolved")):
            raise ContractError(
                f"Issue {identity} is resolved at repair {ordinal}, but retained successful "
                "assessment evidence cannot attribute the reopening; refusing before mutation"
            )
    provenance = ReviewerProvenance(
        reviewer_role=role, attempt_id=attempt_id, candidate_sha=candidate_sha,
        verdict_sha256=digest, evidence_digest=evidence_digest,
        verdict_path=str(path), transaction=key, ownership_valid=True,
        dispatch_evidence=True, completion_evidence=True,
    )
    plan = plan_role_routing(base_issues, verdict, manifest, role, provenance=provenance)
    legacy_reconciliations = []
    for identity, issue in base_issues.items():
        previous = state.issues.get(identity)
        if previous is None:
            continue
        for index, assignment in enumerate(issue.assignments):
            old = previous.assignments[index]
            if assignment.failed_repairs <= old.failed_repairs:
                continue
            added = sorted(set(assignment.failed_repair_ordinals or [])
                           - set(old.failed_repair_ordinals or []))
            records = evidence.get((identity, index), {})
            reconciled = [ordinal for ordinal in added
                          if records.get(ordinal, {}).get("pending_legacy_charges")]
            if len(reconciled) != assignment.failed_repairs - old.failed_repairs:
                raise ContractError(
                    f"Issue {identity} assignment {index} has a legacy counter change without "
                    "unambiguous retained accounting evidence"
                )
            for ordinal in reconciled:
                sources = records[ordinal]["pending_legacy_charges"]
                legacy_reconciliations.append({
                    "issue": identity, "assignment_index": index,
                    "repair_ordinal": ordinal, "source_transactions": sorted(sources),
                })
    binding = "candidate-sha" if require_candidate_binding else "legacy-attempt"
    operation = {
        "status": "intent",
        "role": role,
        "candidate_sha": candidate_sha,
        "evidence_digest": evidence_digest,
        "attempt_id": attempt_id,
        "verdict_sha256": digest,
        "verdict_path": str(path),
        "binding": binding,
        "recorded_at": timestamp(),
        "verdict": verdict.model_dump(mode="json"),
        "base_issues": {identity: issue.model_dump(mode="json")
                        for identity, issue in base_issues.items()},
        "application_base_issues": {identity: issue.model_dump(mode="json")
                                    for identity, issue in state.issues.items()},
        "accounting_decisions": plan.accounting_decisions,
        "legacy_reconciliations": legacy_reconciliations,
        "entry_ids": [entry.entry_id for entry in plan.entries],
        "application_id": (review_application_id(key) if plan.entries else None),
    }
    state = store.record_operation(state, key, operation)
    return _finish_review_operation(store, state, key, state.operations[key], manifest)


def review_transaction_gaps(state: RunState, events: list[dict]) -> list[str]:
    """Report incomplete, unaudited or partially applied review transactions.

    Completion of the role transaction is not enough: every planned entry must
    have an applied decision and its stable audit event, and the application must
    be marked complete. An untouched entry is named so resume-before-dispatch can
    act on it instead of treating the verdict as fully applied.
    """
    gaps: list[str] = []
    for key, operation in state.operations.items():
        if not key.startswith(_REVIEW_RESULT_PREFIX):
            continue
        if not isinstance(operation, dict):
            gaps.append(f"{key} (malformed review operation)")
            continue
        status = operation.get("status")
        if status != "completed":
            gaps.append(f"{key} (transaction status {status or 'unknown'})")
            continue
        result = state.review_results.get(key)
        if result is None:
            gaps.append(f"{key} (persisted role result missing)")
            continue
        if result.verdict_sha256 != operation.get("verdict_sha256"):
            gaps.append(f"{key} (role result and operation disagree)")
        accepted = _review_acceptance_events(events, key)
        if len(accepted) != 1:
            gaps.append(f"{key} (acceptance audit event missing or duplicated)")
        elif (accepted[0].get("accounting_decisions") != operation.get("accounting_decisions")
              or accepted[0].get("legacy_reconciliations")
              != operation.get("legacy_reconciliations")):
            gaps.append(f"{key} (acceptance audit accounting provenance disagrees with transaction)")
        entry_ids = operation.get("entry_ids")
        application_id = operation.get("application_id") or (
            review_application_id(key) if entry_ids else None)
        if entry_ids:
            application = state.review_applications.get(application_id)
            if application is None:
                gaps.append(f"{key} (durable review application missing)")
                continue
            if not application.fully_applied:
                missing = [entry.entry_id for entry in application.entries
                           if entry.state != "applied"]
                gaps.append(f"{key} (unapplied entries: {', '.join(missing)})")
            if not application.complete:
                gaps.append(f"{key} (application completion record missing)")
            audits = [event for event in events
                      if event.get("kind") == "review_entry_applied"
                      and event.get("application_id") == application_id]
            if len(audits) != len(application.entries):
                gaps.append(f"{key} (entry audit events missing or duplicated)")
            assessment = classify_review_evidence(
                state, application_id, operation="gap_check", events=events)
            if assessment.classification != "verified":
                detail = ", ".join(
                    assessment.missing_inputs + assessment.missing_digests
                    + assessment.conflicts)
                gaps.append(
                    f"{key} (evidence {assessment.classification}"
                    + (f": {detail}" if detail else "") + ")")
    for key in state.review_results:
        operation = state.operations.get(key)
        if not isinstance(operation, dict) or operation.get("status") != "completed":
            gaps.append(f"{key} (role result has no completed transaction)")
    return sorted(set(gaps))


def resume_review_applications(store: StateStore, state: RunState,
                               manifest: Manifest) -> RunState:
    """Resume every durable but incomplete review application.

    This runs before dispatch and review finalisation. It never launches a
    worker: it reconciles audits, applies only entries whose deterministic
    decisions were already persisted, and completes the application when every
    entry and audit agrees. A contradiction is refused, not repaired.
    """
    for application_id, application in list(state.review_applications.items()):
        if application.complete:
            continue
        operation = state.operations.get(application.transaction)
        if not isinstance(operation, dict):
            raise ContractError(
                f"Review application {application_id} has no durable transaction; "
                "refusing to invent its decisions"
            )
        if operation.get("status") != "completed":
            state, _result = _finish_review_operation(
                store, state, application.transaction, operation, manifest)
            continue
        # A completed transaction with an incomplete application is a supported
        # crash boundary. Replay from its retained base, never current counters.
        base = operation.get("base_issues")
        recorded = operation.get("verdict")
        if not isinstance(base, dict) or not isinstance(recorded, dict):
            raise ContractError(
                f"Review application {application_id} cannot be resumed: "
                "its transaction has no retained base evidence"
            )
        base_issues = {identity: IssueState.model_validate(value)
                       for identity, value in base.items()}
        verdict = Verdict.model_validate(recorded)
        provenance = _operation_provenance(application.transaction, operation)
        plan = plan_role_routing(
            base_issues, verdict, manifest, application.reviewer_role,
            provenance=provenance)
        application_issues = _build_application_issues(
            _recorded_application_issues(operation, state.issues), plan)
        rebuilt = build_review_application(
            state.review_results[application.transaction], verdict, application_issues,
            approved_inputs=application.approved_inputs,
            proposed_decisions={entry.entry_id: entry.decision for entry in plan.entries},
        )
        state = _ensure_review_application(store, state, rebuilt)
        state = _apply_planned_entries(
            store, state, application_id, plan, application_issues)
        state = _apply_unescorted_side_effects(
            store, state, plan, {entry.issue_id for entry in plan.entries})
        state = store.save(state)
    return state


def load_validated_dependencies(manifest: Manifest, plan: Path,
                                state: RunState) -> dict:
    """Return the validated issue-level dependency view, if configured.

    The reducer never parses a guessed path or schema: the physical record must
    be declared by the approved manifest, and W01 validation must accept every
    known ID, owner, shared scope and the acyclic graph. Any contradiction stops
    affected scheduling for reassessment rather than choosing an interpretation.
    """
    if not manifest.dependency_record:
        return {}
    path = within(plan, manifest.dependency_record)
    if not path.is_file():
        raise ContractError(
            f"Dependency record {manifest.dependency_record} is declared but missing: {path}"
        )
    record, _body = load_dependency_record(path)
    worker_scopes = {worker.id: list(worker.allowed_paths)
                     for worker in manifest.workers}
    validate_dependency_record(
        record,
        known_issue_ids=set(state.issues),
        issue_owners={identity: issue.worker for identity, issue in state.issues.items()},
        known_worker_ids=set(worker_scopes),
        approved_scopes=worker_scopes,
    )
    state.dependency_record = record
    return {item.dependent_issue_id: item for item in record.dependencies}


def _verification_evidence_assembled(state: RunState, prerequisite: str) -> bool:
    """Whether a verification prerequisite has assembled, not merely claimed."""
    issue = state.issues.get(prerequisite)
    if issue is not None:
        if issue.lifecycle != "finished":
            return False
        # A passing review result is the assembled acceptance evidence. An
        # earlier worker's handover claim alone is not enough.
        return any(issue.id in result.resolved
                   for result in state.review_results.values())
    return prerequisite in state.artifacts


def issue_dependency_blockers(issue: IssueState, state: RunState,
                              dependencies: dict) -> list[str]:
    """Return human-readable waits that keep one issue from dispatch."""
    dependency = dependencies.get(issue.id)
    if dependency is None:
        return []
    blockers: list[str] = []
    for prerequisite in dependency.prerequisite_issue_ids:
        required = state.issues.get(prerequisite)
        if required is None:
            blockers.append(f"unknown semantic prerequisite {prerequisite}")
        elif required.lifecycle != "finished":
            blockers.append(
                f"semantic prerequisite {prerequisite} is {required.lifecycle}")
    for prerequisite in dependency.verification_prerequisites:
        if not _verification_evidence_assembled(state, prerequisite):
            blockers.append(
                f"verification prerequisite {prerequisite} has no assembled evidence")
    return blockers


def select_dispatchable_issues(issues: list[IssueState], state: RunState,
                               dependencies: dict,
                               active_resources: set[str] | None = None
                               ) -> tuple[list[IssueState], dict[str, list[str]]]:
    """Select ready issues while serialising declared shared resources.

    Waiting consumes no attempt: an issue omitted here is simply left for a
    later wave. At most one issue per shared-resource key is selected, including
    keys already active in another worker's wave.
    """
    active = set(active_resources or ())
    selected: list[IssueState] = []
    waiting: dict[str, list[str]] = {}
    for issue in sorted(issues, key=lambda item: item.id):
        if issue.lifecycle not in _DISPATCHABLE:
            continue
        blockers = issue_dependency_blockers(issue, state, dependencies)
        if blockers:
            waiting[issue.id] = blockers
            continue
        dependency = dependencies.get(issue.id)
        resources = set(dependency.shared_resource_paths) if dependency else set()
        if resources & active:
            waiting[issue.id] = [
                "shared resource serialised: " + ", ".join(sorted(resources & active))]
            continue
        selected.append(issue)
        active.update(resources)
    return selected, waiting


def carry_forward_gate_reason(state: RunState, plan: Path,
                              manifest: Manifest) -> str | None:
    """Return the dispatch refusal reason while carry-forward import is pending.

    The gate is inert unless the approved manifest declares a carry-forward
    inventory. When one is declared, dispatch is allowed only after the importer
    recorded a completed ``carry_forward_import`` operation whose
    ``inventory_digest`` equals the digest of that approved inventory file.
    The digest is read through the committed importer so its canonicalization
    is never re-derived here.
    """
    if manifest.carry_forward_inventory is None:
        return None
    try:
        inventory_path = within(plan, manifest.carry_forward_inventory)
        approved_digest = load_inventory(inventory_path).inventory_digest
    except (ContractError, OSError, ValueError) as exc:
        return (f"{CARRY_FORWARD_IMPORT_PENDING}: approved inventory "
                f"{manifest.carry_forward_inventory!r} is unusable: {exc}")
    operation = state.operations.get(CARRY_FORWARD_IMPORT_OPERATION)
    if not isinstance(operation, dict):
        return f"{CARRY_FORWARD_IMPORT_PENDING}: no durable import operation"
    if operation.get("status") != "completed":
        return (f"{CARRY_FORWARD_IMPORT_PENDING}: import status is "
                f"{operation.get('status')!r}")
    if operation.get("inventory_digest") != approved_digest:
        return (f"{CARRY_FORWARD_IMPORT_PENDING}: imported inventory digest does "
                "not match the approved inventory")
    return None


def require_carry_forward_import(state: RunState, plan: Path,
                                 manifest: Manifest) -> None:
    """Refuse every dispatch until the approved carry-forward import completes."""
    reason = carry_forward_gate_reason(state, plan, manifest)
    if reason is not None:
        raise Escalation(reason)


def promotion_blockers(state: RunState, events: list[dict] | None = None) -> list[str]:
    """Return exact reasons promotion must wait for explicit disposition."""
    events = events or []
    blockers: list[str] = []
    gaps = review_transaction_gaps(state, events)
    blockers.extend(f"review application gap: {gap}" for gap in gaps)
    for issue in sorted(state.issues.values(), key=lambda item: item.id):
        current = issue.lifecycle
        if current == "finished":
            continue
        if current == "escalated":
            blockers.append(
                f"escalated issue {issue.id} has no explicit human disposition")
        else:
            blockers.append(
                f"issue {issue.id} is {current}, not finished or explicitly accepted/deferred")
    return blockers


def plan_issue_ownership(state: RunState, run_dir: Path, manifest: Manifest) -> list[dict]:
    """Compute every legacy ownership assignment without mutating the register.

    Reviewer ownership is never guessed from an ID prefix. Workflow-generated
    check issues are recognised by their exact generated identity. Any other
    unowned issue must be declared as a finding by exactly one reviewer role in
    a retained verdict; a resolved-list mention is not authority. Ambiguous or
    missing evidence refuses the entire migration instead of choosing a role.
    """
    claims: dict[str, dict[str, list[str]]] = {}
    mentions: dict[str, dict[str, list[str]]] = {}
    for path in sorted(run_dir.glob("round-*/verdict-*.md")):
        stem = path.stem.removeprefix("verdict-")
        role = stem.split("-", 1)[0]
        if role not in _REVIEW_ROLES:
            continue
        try:
            verdict, _ = read_markdown(path, Verdict)
        except (ContractError, OSError):
            continue
        if verdict.reviewer != role:
            continue
        for finding in verdict.findings:
            claims.setdefault(finding.id, {}).setdefault(role, []).append(str(path))
        for identity in verdict.resolved:
            mentions.setdefault(identity, {}).setdefault(role, []).append(str(path))

    entries: list[dict] = []
    for identity, issue in state.issues.items():
        if issue.owner_role is not None:
            continue
        if system_issue_identity(identity, manifest):
            entries.append({"issue": identity, "role": "system",
                            "evidence": ["workflow-generated issue identity"]})
            continue
        owners = sorted(claims.get(identity, {}))
        if len(owners) == 1:
            entries.append({"issue": identity, "role": owners[0],
                            "evidence": claims[identity][owners[0]]})
        elif len(owners) > 1:
            raise ContractError(
                f"Cannot determine owner of {identity}: declared as a finding by "
                + ", ".join(owners) + " in retained verdicts"
            )
        else:
            listed = sorted(mentions.get(identity, {}))
            detail = (f"; it appears only in the {', '.join(listed)} resolved list(s), "
                      "which do not establish ownership" if listed else "")
            raise ContractError(
                f"Cannot determine owner of {identity}: no retained verdict declares it "
                f"as a finding{detail}. Preserve the run and review the evidence."
            )
    return entries


def apply_issue_ownership(state: RunState, entries: list[dict]) -> None:
    for entry in entries:
        state.issues[entry["issue"]].owner_role = entry["role"]


def resolve_issue_ownership(state: RunState, run_dir: Path, manifest: Manifest) -> list[dict]:
    """Validate the whole migration, then assign ownership in memory."""
    entries = plan_issue_ownership(state, run_dir, manifest)
    apply_issue_ownership(state, entries)
    return entries


_OWNERSHIP_ROLES = frozenset({"code", "docs", "system"})


def retained_ownership_evidence(events: list[dict]) -> dict[str, str]:
    """Validate all retained ownership audit and return issue -> single role.

    The check is order-independent and considers the complete role set. One
    issue carrying two distinct roles is contradictory regardless of which event
    came first or whether one role matches a proposed owner. Malformed or
    unsupported records refuse with the offending issue/event named. Repeated
    events for the same role are accepted under the documented replay policy.
    """
    recorded: dict[str, dict] = {}
    for event in events:
        if event.get("kind") != "issue_ownership_resolved":
            continue
        issue = event.get("issue")
        role = event.get("role")
        evidence = event.get("evidence")
        event_id = event.get("id")
        if not isinstance(issue, str) or not issue:
            raise ContractError(
                f"Ownership audit event {event_id} has no usable issue identity"
            )
        if role not in _OWNERSHIP_ROLES:
            raise ContractError(
                f"Ownership audit for {issue} event {event_id} has unsupported role "
                f"{role!r}; expected code, docs or system"
            )
        if (not isinstance(evidence, list) or not evidence
                or not all(isinstance(item, str) and item for item in evidence)):
            raise ContractError(
                f"Ownership audit for {issue} event {event_id} has malformed evidence"
            )
        item = recorded.setdefault(issue, {"roles": set(), "events": []})
        item["roles"].add(role)
        item["events"].append(event_id)
    resolved: dict[str, str] = {}
    for issue, item in recorded.items():
        if len(item["roles"]) > 1:
            raise ContractError(
                f"Conflicting ownership audit for {issue}: events "
                f"{', '.join(str(value) for value in item['events'])} record roles "
                f"{', '.join(sorted(item['roles']))}; refusing to choose one"
            )
        resolved[issue] = next(iter(item["roles"]))
    return resolved


def validate_ownership_audit(events: list[dict], entries: list[dict]) -> dict[str, str]:
    """Validate the complete retained audit and every proposed ownership entry."""
    retained = retained_ownership_evidence(events)
    if not isinstance(entries, list):
        raise ContractError("Proposed ownership evidence is not a list")
    proposed: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ContractError("Proposed ownership entry is not a mapping")
        issue, role = entry.get("issue"), entry.get("role")
        evidence = entry.get("evidence")
        if not isinstance(issue, str) or not issue:
            raise ContractError("Proposed ownership entry has no usable issue identity")
        if role not in _OWNERSHIP_ROLES:
            raise ContractError(
                f"Proposed ownership for {issue} has unsupported role {role!r}"
            )
        if (not isinstance(evidence, list) or not evidence
                or not all(isinstance(item, str) and item for item in evidence)):
            raise ContractError(f"Proposed ownership for {issue} has malformed evidence")
        if issue in proposed and proposed[issue] != role:
            raise ContractError(
                f"Proposed ownership for {issue} names both {proposed[issue]} and {role}"
            )
        proposed[issue] = role
    for issue, role in proposed.items():
        previous = retained.get(issue)
        if previous is not None and previous != role:
            raise ContractError(
                f"Ownership audit for {issue} records {previous}, but current evidence "
                f"resolves {role}; refusing to overwrite or extend it"
            )
    return retained


def persist_issue_ownership(store: StateStore, state: RunState, run_dir: Path,
                            manifest: Manifest) -> tuple[list[dict], RunState]:
    """Persist a complete ownership migration with crash-recoverable audit evidence.

    The complete retained audit and every proposed entry are validated before
    any issue, event or state mutation. Audit events are written before the
    ownership state, so an interruption between the two cannot lose a resolution
    permanently: re-entry recomputes the same entries, sees the retained event
    and completes the save. Contradictory audit is never repaired by appending a
    second role.
    """
    entries = plan_issue_ownership(state, run_dir, manifest)
    retained = validate_ownership_audit(store.events(state.run_id), entries)
    if not entries:
        return [], state
    for entry in entries:
        if entry["issue"] not in retained:
            store.event(state.run_id, "issue_ownership_resolved", **entry)
    apply_issue_ownership(state, entries)
    return entries, store.save(state)


def _same_run_artefact(recorded: str, output: Path, run_id: str) -> bool:
    """Compare an artifact path literally or by its run-relative location.

    A preserved run directory may be copied for audited inspection. Its records
    legitimately keep the original absolute prefix; binding the run ID plus the
    relative path keeps the evidence check meaningful without rewriting it.
    """
    recorded_path = Path(recorded)
    try:
        if recorded_path.resolve() == output.resolve():
            return True
    except OSError:
        pass

    def run_relative(path: Path) -> Path | None:
        parts = path.parts
        for index in range(len(parts) - 1):
            if parts[index] == "runs" and parts[index + 1] == run_id:
                return Path(*parts[index + 2:])
        return None

    original = run_relative(recorded_path)
    return original is not None and original == run_relative(output.resolve())


def _legacy_review_attempt(store: StateStore, state: RunState, logical: str,
                           output: Path) -> tuple[str, dict]:
    """Verify a legacy verdict was produced by the current round's recorded attempt."""
    info = state.workers.get(logical) or {}
    attempt = info.get("attempt_id")
    if not isinstance(attempt, str) or not attempt:
        raise ContractError(f"No recorded attempt identity for {logical}")
    recorded = info.get("output")
    if not recorded or not _same_run_artefact(str(recorded), output, state.run_id):
        raise ContractError(
            f"{logical} recorded output {recorded!r} does not match {output}"
        )
    events = store.events(state.run_id)
    related = _attempt_events(events, logical, attempt)
    intents = [event for event in related if event.get("kind") == "dispatch_intent"]
    completed = [event for event in related if event.get("kind") == "agent_completed"]
    if len(intents) != 1 or len(completed) != 1:
        raise ContractError(
            f"{logical}/{attempt} must have exactly one launch intent and completion; "
            f"found {len(intents)} intent(s) and {len(completed)} completion(s)"
        )
    intent_output = intents[0].get("output")
    if intent_output and not _same_run_artefact(str(intent_output), output, state.run_id):
        raise ContractError(f"{logical}/{attempt} launch intent wrote a different verdict path")
    if any(event.get("kind") in _UNCERTAINTY_EVENTS for event in related):
        raise ContractError(f"{logical}/{attempt} has uncertain launch evidence")
    transitions = [event for event in events
                   if event.get("kind") == "transition"
                   and event.get("stage") == "REVIEW"
                   and event.get("cycle") == state.round]
    if not transitions:
        raise ContractError(f"No transition into REVIEW cycle {state.round} is recorded")
    if str(transitions[0].get("time")) > str(intents[0].get("time")):
        raise ContractError(f"{logical}/{attempt} was dispatched before this REVIEW cycle")
    return attempt, {"intent": intents[0]["id"], "completed": completed[0]["id"]}


def _legacy_pre_batch(
    state: RunState,
    verdicts: list[Verdict],
    pre_ids: set[str],
    evidence: dict[tuple[str, int], dict[int, dict[str, list[str]]]],
) -> tuple[dict[str, IssueState], list[str], list[str]]:
    """Invert exactly the rejected legacy combined-routing batch.

    The old router mutated finding counters and created new findings before it
    detected that a docs pass resolved the same IDs. Inverting one review-round
    increment and one failed-repair increment (only where a repair was already
    charged) reconstructs the pre-batch register. Finding IDs absent from the
    independently generated pre-batch snapshot are the batch's new findings and
    are removed. The assessment mark is reconstructed from completed review
    evidence for the pre-batch counters; if the retained evidence cannot
    establish it, reconstruction refuses before any write. The caller must
    additionally verify the rendered register.
    """
    pre = {identity: issue.model_copy(deep=True) for identity, issue in state.issues.items()}
    finding_ids = sorted({finding.id for verdict in verdicts for finding in verdict.findings})
    resolved_ids = sorted({identity for verdict in verdicts for identity in verdict.resolved})
    removed: list[str] = []
    restored: list[str] = []
    for identity in finding_ids:
        issue = pre.get(identity)
        if identity not in pre_ids:
            if issue is None:
                raise ContractError(f"Batch-created finding {identity} is missing from state")
            if issue.owner_role == "system":
                raise ContractError(f"Workflow-owned issue {identity} was changed by a review batch")
            removed.append(identity)
            continue
        if issue is None:
            raise ContractError(f"Pre-batch issue {identity} is missing from state")
        if issue.owner_role == "system":
            raise ContractError(f"Workflow-owned issue {identity} was changed by a review batch")
        if issue.lifecycle not in _DISPATCHABLE:
            raise ContractError(
                f"Issue {identity} is {issue.lifecycle}, not open/in repair; this is not "
                "the expected partially applied review batch"
            )
        assignment = issue.assignments[-1]
        if assignment.review_rounds < 1:
            raise ContractError(f"Issue {identity} has no review round to roll back")
        assignment.review_rounds -= 1
        if assignment.repairs:
            if assignment.failed_repairs < 1:
                raise ContractError(f"Issue {identity} has no failed repair to roll back")
            assignment.failed_repairs -= 1
        elif assignment.failed_repairs != 0:
            raise ContractError(
                f"Issue {identity} has failed repairs without a charged repair; refusing"
            )
        records = evidence.get((identity, len(issue.assignments) - 1), {})
        assignment.assessed_repairs = None
        assignment.assessed_repairs = _derive_assessed_repairs(
            identity, assignment, records)
        restored.append(identity)
    for identity in resolved_ids:
        if identity not in pre_ids:
            raise ContractError(
                f"Resolved issue {identity} is absent from the pre-batch register"
            )
        issue = pre.get(identity)
        if issue is None:
            raise ContractError(f"Resolved issue {identity} is missing from the register")
        if issue.lifecycle not in _DISPATCHABLE:
            raise ContractError(
                f"Issue {identity} is already {issue.lifecycle}; resolutions from this "
                "batch were applied, so the generic batch recovery does not apply"
            )
        if issue.owner_role is None:
            raise ContractError(f"Issue {identity} has no validated owner")
    unexpected = sorted(set(pre) - pre_ids - set(removed))
    if unexpected:
        raise ContractError(
            "Issues appeared after the pre-batch snapshot: " + ", ".join(unexpected)
        )
    missing = sorted(pre_ids - set(pre))
    if missing:
        raise ContractError(
            "Issues from the pre-batch snapshot are missing: " + ", ".join(missing)
        )
    for identity in removed:
        pre.pop(identity, None)
    return pre, removed, sorted(set(restored))


def _snapshot_issue_ids(snapshot: Path) -> set[str]:
    """Read only issue IDs from the retained pre-batch register snapshot."""
    return set(re.findall(r"^- \*\*(.+?)\*\* — status=", snapshot.read_text(encoding="utf-8"),
                          flags=re.MULTILINE))


def _last_escalation_time(events: list[dict]) -> str | None:
    times = [str(event.get("time")) for event in events if event.get("kind") == "escalated"]
    return times[-1] if times else None


def _review_recovery_event(events: list[dict], key: str) -> dict | None:
    matches = [event for event in events
               if event.get("kind") == "review_batch_recovered"
               and event.get("transaction") == key]
    if len(matches) > 1:
        raise ContractError(f"Review batch recovery {key} has multiple audit events")
    return matches[0] if matches else None


def _audit_ownership_entries(store: StateStore, run_id: str, entries: list[dict]) -> None:
    """Validate all retained and proposed ownership audit, then append only missing roles."""
    retained = validate_ownership_audit(store.events(run_id), entries)
    for entry in entries:
        if entry["issue"] not in retained:
            store.event(run_id, "issue_ownership_resolved", **entry)


def _completed_review_recovery(state: RunState, events: list[dict], key: str,
                               operation: dict) -> dict:
    if not isinstance(operation, dict) or operation.get("status") != "completed":
        raise ContractError(f"Review batch recovery {key} is not complete")
    validate_ownership_audit(events, operation.get("ownership_resolved") or [])
    event = _review_recovery_event(events, key)
    if event is None:
        raise ContractError(f"Review batch recovery {key} has no audit event")
    accepted = operation.get("accepted_transaction")
    if not isinstance(accepted, str) or accepted not in state.review_results:
        raise ContractError(f"Review batch recovery {key} has no persisted accepted role result")
    return {
        "status": "already-recovered",
        "transaction": key,
        "audit_event": event["id"],
        "accepted_transaction": accepted,
        "accepted_role": "code",
        "rejected_role": "docs",
        "restored": list(operation.get("restored_ids") or []),
        "removed": list(operation.get("removed_ids") or []),
        "operator": operation.get("operator"),
        "reason": operation.get("reason"),
    }


def recover_review_batch(store: StateStore, feature, manifest: Manifest, plan: Path,
                         run_id: str, *, operator: str, reason: str,
                         dry_run: bool = False) -> dict:
    """Audited recovery of the exact partially applied legacy review batch.

    The helper never resets counters by guesswork. It inverts only the rejected
    batch's known mutations, requires the reconstruction to equal the
    independent pre-batch issue-register copy written before that routing call,
    accepts the valid code role through the normal transaction, and records the
    docs role as rejected without mutating its findings. Re-entry replays the
    recorded decision and completes the same operation.
    """
    operator, reason = operator.strip(), reason.strip()
    if not operator or not reason:
        raise ContractError("Review batch recovery requires a non-empty operator and reason")
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(store.lock("active", timeout=0))
        except portalocker.exceptions.LockException as exc:
            raise ContractError(
                "A live engine holds this run; pause it and wait for the process to exit "
                "before review batch recovery"
            ) from exc
        state = store.load(run_id)
        working = state.model_copy(deep=True) if dry_run else state
        engine = Engine(store, working, feature, manifest, plan)
        engine.state = working
        round_no = working.round
        candidate_data = working.candidate or {}
        candidate_sha = candidate_data.get("sha")
        if not isinstance(candidate_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
            raise ContractError("The run has no immutable candidate SHA; recovery does not apply")
        code_path = engine.run_dir / f"round-{round_no}" / "verdict-code.md"
        docs_path = engine.run_dir / f"round-{round_no}" / "verdict-docs.md"
        for path in (code_path, docs_path):
            if not path.is_file():
                raise ContractError(f"Round-{round_no} verdict evidence is missing: {path}")
        code_verdict, _ = read_markdown(code_path, Verdict)
        docs_verdict, _ = read_markdown(docs_path, Verdict)
        if code_verdict.reviewer != "code" or docs_verdict.reviewer != "docs":
            raise ContractError("Round verdict reviewer identities are wrong")
        for verdict, sha in ((code_verdict, candidate_sha), (docs_verdict, candidate_sha)):
            if verdict.candidate_sha is not None and verdict.candidate_sha != sha:
                raise ContractError("A round verdict names a different candidate")
        if approval_hashes(plan, manifest, feature) != working.approved_hashes:
            raise ContractError(
                "Approved inputs changed since this candidate was reviewed; run orch approve "
                "and review the candidate again instead of recovering this batch"
            )
        docs_digest = _file_sha256(docs_path)
        key = f"{_REVIEW_BATCH_RECOVERY_PREFIX}{round_no}:{candidate_sha}:{docs_digest}"
        existing = working.operations.get(key)
        if existing is not None:
            if not isinstance(existing, dict):
                raise ContractError(f"Review batch recovery {key} is malformed")
            status = existing.get("status")
            if status == "completed":
                return _completed_review_recovery(working, store.events(run_id), key, existing)
            if status != "intent":
                raise ContractError(f"Review batch recovery {key} has unknown status {status!r}")
            if working.phase not in {"ESCALATED", "PAUSED"}:
                raise ContractError(
                    f"Run phase {working.phase} does not permit review batch recovery replay"
                )
            if (existing.get("candidate_sha") != candidate_sha
                    or existing.get("docs_verdict_sha256") != docs_digest):
                raise ContractError(f"Review batch recovery {key} conflicts with current evidence")
            if _file_sha256(code_path) != existing.get("code_verdict_sha256"):
                raise ContractError("The code verdict changed after the recovery decision")
            snapshot_path = Path(str(existing.get("snapshot_path")))
            if not snapshot_path.is_file() or _file_sha256(snapshot_path) != existing.get("snapshot_sha256"):
                raise ContractError("The pre-batch issue-register snapshot changed or is missing")
            try:
                pre = {identity: IssueState.model_validate(value)
                       for identity, value in (existing.get("pre_issues") or {}).items()}
            except (ValueError, TypeError) as exc:
                raise ContractError(f"Recorded pre-batch issue snapshot is malformed: {exc}") from exc
            removed = list(existing.get("removed_ids") or [])
            restored = list(existing.get("restored_ids") or [])
            invalid_resolved = list(existing.get("invalid_resolved") or [])
            operator = str(existing.get("operator"))
            reason = str(existing.get("reason"))
            summary = {
                "status": "replayed",
                "transaction": key,
                "operator": existing.get("operator"),
                "reason": existing.get("reason"),
                "round": round_no,
                "candidate_sha": candidate_sha,
                "accepted_role": "code",
                "rejected_role": "docs",
                "restored": restored,
                "removed": removed,
                "invalid_resolved": invalid_resolved,
                "snapshot": str(snapshot_path),
                "code_attempt": existing.get("code_attempt"),
                "docs_attempt": existing.get("docs_attempt"),
                "ownership_resolved": list(existing.get("ownership_resolved") or []),
            }
            if dry_run:
                summary["status"] = "dry-run"
                return summary
        else:
            if working.phase != "ESCALATED" or working.stage != "REVIEW":
                raise ContractError(
                    f"Review batch recovery requires an escalated REVIEW stage, "
                    f"not {working.phase}/{working.stage}"
                )
            if working.error != _LEGACY_REVIEW_CONFLICT:
                raise ContractError(
                    f"Review batch recovery requires the recorded cross-role conflict; "
                    f"found {working.error!r}"
                )
            if working.pending_control:
                raise ContractError("A pending control request blocks review batch recovery")
            from orch.git_ops import Candidate
            candidate = Candidate(**{**candidate_data, "path": Path(candidate_data["path"])})
            engine.verify_candidate(candidate)
            code_attempt, code_evidence_ids = _legacy_review_attempt(
                store, working, "review-code", code_path
            )
            docs_attempt, docs_evidence_ids = _legacy_review_attempt(
                store, working, "review-docs", docs_path
            )
            ownership_entries = engine.settle_issue_ownership(persist=False)
            validate_ownership_audit(store.events(run_id), ownership_entries)

            snapshots = sorted((engine.run_dir / f"round-{round_no}" / "docs-review")
                               .glob("*-issues.md"))
            if len(snapshots) != 1:
                raise ContractError(
                    "Expected exactly one pre-batch issue-register snapshot in "
                    f"round-{round_no}/docs-review"
                )
            snapshot = snapshots[0]
            evidence = repair_assessment_evidence(working)
            pre, removed, restored = _legacy_pre_batch(
                working, [code_verdict, docs_verdict], _snapshot_issue_ids(snapshot),
                evidence,
            )
            plan_role_routing(pre, code_verdict, manifest, "code")
            try:
                plan_role_routing(pre, docs_verdict, manifest, "docs")
            except ContractError as exc:
                docs_error = str(exc)
            else:
                raise ContractError(
                    "The documentation verdict is valid; this recovery handles only a "
                    "cross-role resolution rejection"
                )
            if docs_verdict.result != "pass" or "may not resolve" not in docs_error:
                raise ContractError(
                    f"The documentation verdict is not the expected cross-role rejection: {docs_error}"
                )
            invalid_resolved = sorted(
                identity for identity in docs_verdict.resolved
                if (pre.get(identity) is not None
                    and pre[identity].owner_role != "docs")
            )
            if not invalid_resolved:
                raise ContractError("No cross-role resolutions were found to reject")

            snapshot_state = working.model_copy(deep=True)
            snapshot_state.issues = pre
            expected = issues_markdown(snapshot_state, include_ownership=False)
            if snapshot.read_text(encoding="utf-8") != expected:
                raise ContractError(
                    "The reconstructed pre-batch issue register does not match "
                    f"{snapshot}; refusing recovery"
                )
            escalation_time = _last_escalation_time(store.events(run_id))
            if escalation_time is None:
                raise ContractError("No escalation event records the rejected batch time")
            snapshot_time = datetime.fromtimestamp(snapshot.stat().st_mtime, UTC)
            if snapshot_time.timestamp() > datetime.fromisoformat(escalation_time).timestamp() + 2:
                raise ContractError("The issue-register snapshot postdates the rejected batch")
            summary = {
                "status": "dry-run" if dry_run else "recovered",
                "transaction": key,
                "operator": operator,
                "reason": reason,
                "round": round_no,
                "candidate_sha": candidate_sha,
                "accepted_role": "code",
                "rejected_role": "docs",
                "restored": restored,
                "removed": removed,
                "invalid_resolved": invalid_resolved,
                "snapshot": str(snapshot),
                "code_attempt": code_attempt,
                "docs_attempt": docs_attempt,
                "code_evidence_events": code_evidence_ids,
                "docs_evidence_events": docs_evidence_ids,
            }
            if dry_run:
                return summary

            operation = {
                "status": "intent",
                "operator": operator,
                "reason": reason,
                "round": round_no,
                "candidate_sha": candidate_sha,
                "pre_issues": {identity: issue.model_dump(mode="json")
                               for identity, issue in pre.items()},
                "removed_ids": removed,
                "restored_ids": restored,
                "snapshot_path": str(snapshot),
                "snapshot_sha256": _file_sha256(snapshot),
                "code_verdict_path": str(code_path),
                "code_verdict_sha256": _file_sha256(code_path),
                "docs_verdict_path": str(docs_path),
                "docs_verdict_sha256": docs_digest,
                "invalid_resolved": invalid_resolved,
                "code_attempt": code_attempt,
                "docs_attempt": docs_attempt,
                "ownership_resolved": ownership_entries,
                "recorded_at": timestamp(),
            }
            working = store.record_operation(working, key, operation)
            engine.state = working
            _audit_ownership_entries(store, working.run_id, ownership_entries)
            summary["ownership_resolved"] = ownership_entries

        # Common completion path. Prefer the engine's normal evidence digest so
        # the resumed reviewer computes the same key and reuses the result.
        from orch.git_ops import Candidate
        candidate = Candidate(**{**candidate_data, "path": Path(candidate_data["path"])})
        engine.verify_candidate(candidate)
        _cwd, inputs, _ = engine.review_context("code", candidate)
        evidence_digest = engine.review_evidence_digest("code", candidate, inputs)
        code_key = review_operation_key("code", candidate_sha, evidence_digest)
        validate_ownership_audit(store.events(run_id),
                                 summary.get("ownership_resolved") or [])
        code_operation = working.operations.get(code_key)
        if (isinstance(code_operation, dict)
                and code_operation.get("status") == "completed"):
            if _issues_digest(working.issues) != code_operation.get("after_issues_digest"):
                raise ContractError(
                    "The accepted code result does not match the current issue register; "
                    "refusing to complete review batch recovery"
                )
        else:
            if dry_run:
                summary["planned_transaction"] = code_key
                return summary
            for identity in removed:
                working.issues.pop(identity, None)
            for identity in restored:
                if identity in pre:
                    working.issues[identity] = pre[identity].model_copy(deep=True)
            working = store.save(working)
            engine.state = working

        if summary.get("ownership_resolved"):
            _audit_ownership_entries(store, run_id, summary["ownership_resolved"])
        events = store.events(run_id)
        rejections = [event for event in events
                      if event.get("kind") == "review_result_rejected"
                      and event.get("recovery") == key and event.get("role") == "docs"]
        if len(rejections) > 1:
            raise ContractError(f"Review batch recovery {key} has duplicate rejection events")
        if not rejections:
            store.event(
                run_id, "review_result_rejected", role="docs", recovery=key,
                attempt_id=str(summary.get("docs_attempt")),
                verdict_path=str(docs_path), verdict_sha256=docs_digest,
                reason=(
                    "Documentation verdict resolved code-owned findings and cannot be "
                    "accepted; no issue state or counter was changed for this role"
                ),
                invalid_resolved=invalid_resolved,
            )

        working, code_result = accept_review_result(
            store, working, role="code", candidate_sha=candidate_sha,
            evidence_digest=evidence_digest,
            attempt_id=str(summary.get("code_attempt")),
            verdict=code_verdict, verdict_path=code_path, manifest=manifest,
            require_candidate_binding=False,
        )
        engine.state = working
        events = store.events(run_id)
        event = _review_recovery_event(events, key)
        if event is None:
            event = store.event(
                run_id, "review_batch_recovered", transaction=key, operator=operator,
                reason=reason, round=round_no, candidate_sha=candidate_sha,
                accepted_transaction=code_result.transaction,
                rejected_role="docs", rejected_verdict_sha256=docs_digest,
                restored=restored, removed=removed,
                invalid_resolved=invalid_resolved,
                snapshot=str(summary.get("snapshot")),
                snapshot_sha256=working.operations[key].get("snapshot_sha256"),
            )
        working.error = None
        working = store.record_operation(working, key, {
            "status": "completed", "event_id": event["id"],
            "accepted_transaction": code_result.transaction,
            "completed_at": timestamp(),
        })
        summary.update({
            "status": "recovered",
            "audit_event": event["id"],
            "accepted_transaction": code_result.transaction,
            "code_result": code_result.result,
            "docs_result": "rejected; pending retry",
        })
        return summary


def _attempt_events(events: list[dict], agent: str, attempt: str) -> list[dict]:
    return [event for event in events
            if event.get("agent") == agent and event.get("attempt") == attempt]


def _recovery_evidence(events: list[dict], attempt_id: str) -> list[dict]:
    """Validate attempt-scoped prelaunch and uncertainty evidence.

    Resolution events are excluded so replay can re-validate the same source
    evidence. Any process, terminal, duplicate-intent or missing-uncertainty
    evidence raises instead of being discarded.
    """
    source = [event for event in events
              if event.get("kind") != "uncertain_attempt_resolved"]
    if any(event.get("kind") in _PROCESS_EVENT_KINDS for event in source):
        raise ContractError(
            f"Attempt {attempt_id} has durable process identity evidence; "
            "it requires escalation, not manual recovery"
        )
    if any(event.get("kind") in _ATTEMPT_TERMINAL_EVENTS for event in source):
        raise ContractError(f"Attempt {attempt_id} already has terminal evidence")
    intents = [event for event in source if event.get("kind") == "dispatch_intent"]
    if len(intents) != 1:
        raise ContractError(
            f"Attempt {attempt_id} must have exactly one launch intent; found {len(intents)}"
        )
    if not any(event.get("kind") in _UNCERTAINTY_EVENTS for event in source):
        raise ContractError(
            f"Attempt {attempt_id} has no recognized uncertainty event "
            "(agent_launch_uncertain or agent_interruption_uncertain)"
        )
    return source


def _require_recovery_state(info: dict | None, attempt_id: str) -> None:
    """Refuse contradictory or identity-bearing worker state without editing it."""
    if info is None:
        raise ContractError(f"Worker state for attempt {attempt_id} is missing")
    if info.get("attempt_id") != attempt_id:
        raise ContractError(
            f"Worker attempt identity {info.get('attempt_id')!r} does not match {attempt_id}"
        )
    recorded = [name for name in ("process", "process_attempt_id")
                if info.get(name) is not None]
    if recorded:
        raise ContractError(
            f"Worker state for {attempt_id} records {', '.join(recorded)}; "
            "refusing recovery and leaving that identity inspectable"
        )
    marker = info.get("uncertain_attempt_id")
    if marker not in (None, attempt_id):
        raise ContractError(
            f"Worker state points at uncertain attempt {marker!r}, not {attempt_id}"
        )
    status = info.get("status")
    if marker == attempt_id and status not in {"uncertain", "interrupted"}:
        raise ContractError(
            f"Worker {attempt_id} is contradictory: status {status!r} with an uncertainty marker"
        )
    if marker is None and status not in {"interrupted", "uncertain"}:
        raise ContractError(
            f"Worker {attempt_id} is not in a recoverable state: status {status!r}"
        )


def _same_recovery_decision(entry: dict, operator: str, reason: str,
                            evidence_ids: list[int]) -> bool:
    return (
        entry.get("operator") == operator
        and entry.get("reason") == reason
        and sorted(entry.get("evidence_event_ids") or []) == sorted(evidence_ids)
        and entry.get("process_identity_events", 0) == 0
    )


def _matching_resolution_event(events: list[dict], name: str, attempt_id: str,
                               operator: str, reason: str,
                               evidence_ids: list[int]) -> dict | None:
    audits = [event for event in _attempt_events(events, name, attempt_id)
              if event.get("kind") == "uncertain_attempt_resolved"]
    if len(audits) > 1:
        raise ContractError(
            f"Attempt {attempt_id} has multiple resolution events; refusing recovery"
        )
    if not audits:
        return None
    event = audits[0]
    if (event.get("operator") != operator or event.get("reason") != reason
            or sorted(event.get("evidence_event_ids") or []) != sorted(evidence_ids)):
        raise ContractError(
            f"Existing resolution event for {attempt_id} conflicts with the recorded decision"
        )
    return event


def _completed_recovery_event(state: RunState, events: list[dict], key: str) -> dict:
    attempt_id = key.removeprefix(_RECOVERY_OPERATION_PREFIX)
    operation = state.operations.get(key)
    if not isinstance(operation, dict):
        raise ContractError(f"Recovery operation {key} is malformed")
    name, event_id = operation.get("worker"), operation.get("event_id")
    evidence_ids = operation.get("evidence_event_ids")
    if (not isinstance(name, str) or not isinstance(event_id, int)
            or not isinstance(operation.get("operator"), str)
            or not isinstance(operation.get("reason"), str)
            or not isinstance(evidence_ids, list)
            or not all(isinstance(item, int) for item in evidence_ids)):
        raise ContractError(
            f"Recovery operation {key} has an incomplete completion record"
        )
    matching = [event for event in events if event.get("id") == event_id]
    if len(matching) != 1:
        raise ContractError(f"Recovery operation {key} has no unique audit event")
    event = matching[0]
    if (event.get("kind") != "uncertain_attempt_resolved"
            or event.get("agent") != name or event.get("attempt") != attempt_id):
        raise ContractError(f"Recovery operation {key} references a conflicting audit event")
    _matching_resolution_event(events, name, attempt_id, operation["operator"],
                               operation["reason"], evidence_ids)
    info = state.workers.get(name) or {}
    if info.get("uncertain_attempt_id") == attempt_id:
        raise ContractError(
            f"Recovery for {attempt_id} is recorded complete but the uncertainty marker remains"
        )
    return event


def _finish_uncertain_recovery(store: StateStore, state: RunState, *, key: str, name: str,
                               attempt_id: str, operator: str, reason: str,
                               evidence_ids: list[int]) -> dict:
    """Write the audit event, then the resolution state, then the completion.

    Replay at every boundary reuses the recorded decision and existing event;
    it never appends a second decision and never trusts changed arguments.
    """
    events = store.events(state.run_id)
    source = _recovery_evidence(_attempt_events(events, name, attempt_id), attempt_id)
    if sorted(event["id"] for event in source) != sorted(evidence_ids):
        raise ContractError(
            f"Attempt {attempt_id} evidence changed since the operator decision; "
            "refusing to complete recovery"
        )
    info = state.workers.get(name)
    _require_recovery_state(info, attempt_id)
    event = _matching_resolution_event(
        events, name, attempt_id, operator, reason, evidence_ids
    )
    if event is None:
        event = store.event(
            state.run_id, "uncertain_attempt_resolved", agent=name, attempt=attempt_id,
            operator=operator, reason=reason, evidence_event_ids=list(evidence_ids),
            process_identity_events=0,
        )
    entries = info.get("uncertain_attempt_resolutions", [])
    if not isinstance(entries, list):
        raise ContractError(
            f"Resolution history for {attempt_id} is malformed; refusing recovery"
        )
    entries = [entry for entry in entries
               if isinstance(entry, dict) and entry.get("attempt") == attempt_id]
    if len(entries) > 1:
        raise ContractError(
            f"Attempt {attempt_id} has multiple resolution history entries; refusing recovery"
        )
    changed = False
    if entries:
        if not _same_recovery_decision(entries[0], operator, reason, evidence_ids):
            raise ContractError(
                f"Resolution history for {attempt_id} conflicts with the recorded decision"
            )
    else:
        info.setdefault("uncertain_attempt_resolutions", []).append({
            "attempt": attempt_id, "operator": operator, "reason": reason,
            "resolved_at": timestamp(), "evidence_event_ids": list(evidence_ids),
            "process_identity_events": 0,
        })
        changed = True
    if info.get("uncertain_attempt_id") == attempt_id:
        info.pop("uncertain_attempt_id")
        changed = True
    if info.get("status") != "interrupted":
        info["status"] = "interrupted"
        changed = True
    if changed:
        info["activity"] = timestamp()
        state = store.save(state)
    operation = state.operations.get(key)
    if not isinstance(operation, dict):
        operation = {}
    if operation.get("status") != "completed":
        store.record_operation(state, key, {"status": "completed", "event_id": event["id"]})
    return event


def resolve_uncertain_dispatch(store: StateStore, run_id: str, attempt_id: str, *,
                               operator: str, reason: str) -> dict:
    """Audited, operator-controlled resolution of one uncertain dispatch.

    A fresh decision is persisted as an operation intent before uncertainty can
    be cleared. Re-entry for an existing operation uses the original durable
    operator, reason and evidence, finishes the same operation, and never asks
    for or records a second decision. The helper refuses journal or worker-state
    process identity, terminal evidence, duplicate intents, contradictory state
    and changed evidence rather than deleting or relabelling any of it.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", attempt_id):
        raise ContractError(f"Unsafe attempt identity: {attempt_id!r}")
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(store.lock("active", timeout=0))
        except portalocker.exceptions.LockException as exc:
            raise ContractError(
                "A live engine holds this run; pause it and wait for the process to exit "
                "before audited recovery"
            ) from exc
        state = store.load(run_id)
        events = store.events(run_id)
        key = f"{_RECOVERY_OPERATION_PREFIX}{attempt_id}"
        existing = state.operations.get(key)
        if existing is not None:
            if not isinstance(existing, dict):
                raise ContractError(f"Recovery operation {key} is malformed")
            status = existing.get("status")
            if status == "completed":
                return _completed_recovery_event(state, events, key)
            if status != "intent":
                raise ContractError(
                    f"Recovery operation {key} has unknown status {status!r}; refusing"
                )
            if state.phase not in {"PAUSED", "ESCALATED", "APPROVED"}:
                raise ContractError(
                    f"Run phase {state.phase} does not permit recovery replay"
                )
            name = existing.get("worker")
            recorded_operator = existing.get("operator")
            recorded_reason = existing.get("reason")
            evidence_ids = existing.get("evidence_event_ids")
            if (not isinstance(name, str) or not name
                    or not isinstance(recorded_operator, str) or not recorded_operator
                    or not isinstance(recorded_reason, str) or not recorded_reason
                    or not isinstance(evidence_ids, list) or not evidence_ids
                    or not all(isinstance(item, int) for item in evidence_ids)):
                raise ContractError(
                    f"Recorded recovery decision for {attempt_id} is incomplete; refusing replay"
                )
            return _finish_uncertain_recovery(
                store, state, key=key, name=name, attempt_id=attempt_id,
                operator=recorded_operator, reason=recorded_reason,
                evidence_ids=evidence_ids,
            )

        operator, reason = operator.strip(), reason.strip()
        if not operator or not reason:
            raise ContractError("Audited recovery requires a non-empty operator and reason")
        if state.phase not in {"PAUSED", "ESCALATED"}:
            raise ContractError(
                f"Run phase {state.phase} does not permit audited recovery"
            )
        matches = [(name, info) for name, info in state.workers.items()
                   if info.get("uncertain_attempt_id") == attempt_id]
        if len(matches) != 1:
            raise ContractError(f"No single recorded uncertain dispatch {attempt_id}")
        name, _ = matches[0]
        source = _recovery_evidence(_attempt_events(events, name, attempt_id), attempt_id)
        _require_recovery_state(state.workers.get(name), attempt_id)
        evidence_ids = [event["id"] for event in source]
        state = store.record_operation(state, key, {
            "status": "intent", "worker": name, "attempt": attempt_id,
            "operator": operator, "reason": reason,
            "evidence_event_ids": evidence_ids,
        })
        return _finish_uncertain_recovery(
            store, state, key=key, name=name, attempt_id=attempt_id,
            operator=operator, reason=reason, evidence_ids=evidence_ids,
        )


def _human_disposition_operation_key(issue_id: str,
                                     transition: LifecycleTransition) -> str:
    """Stable operation identity for one retained human-disposition transition."""
    return (f"{_HUMAN_DISPOSITION_OPERATION_PREFIX}{issue_id}:"
            f"{transition.from_status}->{transition.to_status}:"
            f"{transition.recorded_at}")


def _accounting_digest(issue: IssueState) -> str:
    """Digest counters and assignment history, excluding lifecycle status."""
    payload = {
        "attempt": issue.attempt,
        "assignments": [assignment.model_dump(mode="json")
                        for assignment in issue.assignments],
    }
    return _sha256_text(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _human_disposition_expectation(issue: IssueState,
                                   retained: LifecycleTransition,
                                   disposition: HumanDisposition) -> LifecycleTransition:
    """Revalidate a disposition against the retained transition without mutating it.

    The pre-transition issue is reconstructed in memory and passed through the
    contract layer. A mismatch in guard or evidence means the supplied
    disposition is not the one that produced the retained transition.
    """
    if not issue.lifecycle_history or issue.lifecycle_history[-1] is not retained:
        raise ContractError(
            f"Issue {issue.id} retained transition is not the latest lifecycle entry")
    if issue.status != retained.to_status:
        raise ContractError(
            f"Issue {issue.id} currently records {issue.status!r}, not the retained "
            f"transition result {retained.to_status!r}; refusing reconciliation")
    probe = issue.model_copy(deep=True)
    probe.status = retained.from_status
    probe.lifecycle_history = probe.lifecycle_history[:-1]
    try:
        expected = apply_human_disposition(probe, disposition)
    except LifecycleTransitionError as exc:
        raise ContractError(
            f"Human disposition for {disposition.issue_id} does not match the retained "
            f"transition: {exc}") from exc
    if (expected.from_status, expected.to_status, expected.guard, expected.evidence) != (
            retained.from_status, retained.to_status, retained.guard, retained.evidence):
        raise ContractError(
            f"Human disposition for {disposition.issue_id} does not match the retained "
            "transition guard or evidence; refusing reconciliation")
    if expected.to_status == "finished" and (
            (disposition.kind == "deferral") != (disposition.deferred_stage is not None)):
        raise ContractError(
            f"Human disposition for {disposition.issue_id} has a deferred stage that "
            "cannot distinguish scoped acceptance from deferral; refusing ambiguous "
            "reconciliation")
    return expected


def _matching_human_disposition_audit(events: list[dict], issue_id: str,
                                      transition: LifecycleTransition) -> dict | None:
    """Return the one audit for this transition, refusing ambiguous attribution."""
    audits = [event for event in events
              if event.get("kind") == "human_disposition"
              and event.get("issue") == issue_id]
    identified = [event for event in audits
                  if event.get("transition_recorded_at") == transition.recorded_at]
    if len(identified) > 1:
        raise ContractError(
            f"Human disposition for {issue_id} has {len(identified)} audits for the "
            "same retained transition; refusing duplicate reconciliation")
    if identified:
        return identified[0]
    lookalikes = [event for event in audits
                  if event.get("from_status") == transition.from_status
                  and event.get("to_status") == transition.to_status
                  and list(event.get("evidence") or []) == list(transition.evidence)]
    if lookalikes:
        raise ContractError(
            f"Human disposition for {issue_id} has an audit with matching content but "
            "no usable transition identity; refusing ambiguous reconciliation")
    return None


def _validate_human_disposition_audit(event: dict, disposition: HumanDisposition,
                                      transition: LifecycleTransition) -> None:
    expected = {
        "issue": disposition.issue_id,
        "disposition_kind": disposition.kind,
        "from_status": transition.from_status,
        "to_status": transition.to_status,
        "scope": disposition.scope,
        "evidence": list(transition.evidence),
        "deferred_stage": disposition.deferred_stage,
    }
    for field, value in expected.items():
        if event.get(field) != value:
            raise ContractError(
                f"Existing human_disposition audit {event.get('id')} for "
                f"{disposition.issue_id} disagrees on {field}; refusing reconciliation")


def _validate_human_disposition_operation(operation: dict, key: str,
                                          disposition: HumanDisposition,
                                          transition: LifecycleTransition,
                                          accounting_digest: str) -> None:
    expected = {
        "issue": disposition.issue_id,
        "disposition_kind": disposition.kind,
        "scope": disposition.scope,
        "evidence": list(disposition.evidence),
        "allowance_disposition": disposition.allowance_disposition,
        "dependencies": list(disposition.dependencies),
        "deferred_stage": disposition.deferred_stage,
        "from_status": transition.from_status,
        "to_status": transition.to_status,
        "transition_recorded_at": transition.recorded_at,
        "transition_guard": transition.guard,
        "transition_evidence": list(transition.evidence),
    }
    for field, value in expected.items():
        if operation.get(field) != value:
            raise ContractError(
                f"Human disposition operation {key} disagrees on {field}; refusing "
                "reconciliation")
    if operation.get("status") not in {"intent", "completed"}:
        raise ContractError(
            f"Human disposition operation {key} has unknown status "
            f"{operation.get('status')!r}; refusing reconciliation")
    for field in ("prior_accounting_digest", "accounting_digest"):
        recorded = operation.get(field)
        if recorded != accounting_digest:
            raise ContractError(
                f"Human disposition operation {key} records {field} {recorded!r}, but "
                f"issue {disposition.issue_id} now has {accounting_digest}; counters or "
                "assignments changed and cannot be reconciled")


def reconcile_human_disposition(store: StateStore, state: RunState,
                                disposition: HumanDisposition, *,
                                expected_accounting_digest: str | None = None
                                ) -> tuple[RunState, dict]:
    """Append only the missing audit for an already-saved human disposition.

    The transition itself is never reapplied: status, counters, assignments and
    lifecycle history are read and validated but not written. A finished status
    alone is never sufficient; the exact retained transition must match the
    supplied authorized disposition. Repeated reconciliation returns the
    existing audit instead of appending another.
    """
    issue = state.issues.get(disposition.issue_id)
    if issue is None:
        raise ContractError(
            f"Human disposition names unknown issue {disposition.issue_id}")
    history = issue.lifecycle_history
    if not history:
        raise ContractError(
            f"Issue {issue.id} has no retained lifecycle transition; a finished "
            "status alone is not evidence of an authorized human disposition")
    retained = history[-1]
    matches = [transition for transition in history if (
        transition.from_status, transition.to_status, transition.guard,
        transition.evidence) == (
        retained.from_status, retained.to_status, retained.guard,
        retained.evidence)]
    if len(matches) != 1:
        raise ContractError(
            f"Issue {issue.id} has {len(matches)} retained transitions matching the "
            "supplied disposition; attribution is ambiguous and cannot be reconciled")
    _human_disposition_expectation(issue, retained, disposition)
    accounting_digest = _accounting_digest(issue)
    if (expected_accounting_digest is not None
            and accounting_digest != expected_accounting_digest):
        raise ContractError(
            f"Issue {issue.id} accounting digest {accounting_digest} does not match "
            "the supplied pre-transition snapshot; counters or assignments changed")
    key = _human_disposition_operation_key(issue.id, retained)
    operation = state.operations.get(key)
    if operation is not None:
        if not isinstance(operation, dict):
            raise ContractError(
                f"Human disposition operation {key} is malformed; refusing reconciliation")
        _validate_human_disposition_operation(
            operation, key, disposition, retained, accounting_digest)
    event = _matching_human_disposition_audit(
        store.events(state.run_id), issue.id, retained)
    if event is not None:
        _validate_human_disposition_audit(event, disposition, retained)
        if operation is not None and operation.get("status") != "completed":
            state = store.record_operation(state, key, {
                "status": "completed",
                "audit_event_id": event["id"],
                "reconciled": bool(event.get("reconciled")),
                "reconciled_at": event.get("reconciled_at"),
            })
        return state, event
    reconciled_at = timestamp()
    details = {
        "issue": issue.id,
        "disposition_kind": disposition.kind,
        "from_status": retained.from_status,
        "to_status": retained.to_status,
        "scope": disposition.scope,
        "evidence": list(retained.evidence),
        "deferred_stage": disposition.deferred_stage,
        "transition_recorded_at": retained.recorded_at,
        "reconciled": True,
        "reconciled_at": reconciled_at,
        "accounting_digest": accounting_digest,
    }
    if operation is not None:
        details["operation"] = key
    event = store.event(state.run_id, "human_disposition", **details)
    if operation is not None:
        state = store.record_operation(state, key, {
            "status": "completed",
            "audit_event_id": event["id"],
            "reconciled": True,
            "reconciled_at": reconciled_at,
        })
    return state, event


class Engine:
    def __init__(self, store: StateStore, state: RunState, feature: FeatureSpec,
                 manifest: Manifest, plan: Path, *, adapters=None, runner=None,
                 config: Config | None = None, settings_loader=None):
        from orch.agents import CodexAdapter, OpenCodeAdapter
        from orch.git_ops import GitWorkspace, WorkspaceRecord
        from orch.processes import ProcessRunner
        self.store, self.state, self.feature = store, state, feature
        self.config = config or Config()
        self.manifest, self.plan = manifest, plan.resolve()
        self.settings_loader = settings_loader
        self.run_dir = store.run_dir(state.run_id)
        self.git = GitWorkspace(feature.target_project, self.run_dir)
        self.record = WorkspaceRecord.from_dict(state.workspace)
        self.runner = runner or ProcessRunner()
        self.adapters = adapters or {"planner": CodexAdapter(self.runner), "opencode": OpenCodeAdapter(self.runner)}
        # Independent roles may finish concurrently; their durable commits are
        # serialised and each reloads the latest revision first.
        self._review_commit_lock = asyncio.Lock()

    def save(self):
        saved = self.store.save(self.state)
        self.state.revision = saved.revision

    def event(self, kind, **details):
        return self.store.event(self.state.run_id, kind, **details)

    def settle_issue_ownership(self, persist: bool = True) -> list[dict]:
        """Resolve legacy ownership from retained verdict evidence.

        A non-persisting call validates the whole migration and applies it only
        in memory. A persisting call writes the complete audit evidence before
        ownership state, so re-entry after an interruption can finish the same
        migration without losing or duplicating resolution events.
        """
        if not persist:
            return resolve_issue_ownership(self.state, self.run_dir, self.manifest)
        entries, saved = persist_issue_ownership(
            self.store, self.state, self.run_dir, self.manifest)
        if entries:
            self.state = saved
        return entries

    def verify_candidate(self, candidate):
        """Refuse to trust a mutable candidate while or after a reviewer reads it."""
        from orch.git_ops import git, head

        if head(candidate.path) != candidate.sha or git(candidate.path, "status", "--porcelain").strip():
            raise Escalation("Reviewer changed the candidate; verdicts invalidated")

    def review_context(self, role, candidate):
        """Return the role-isolated working directory, inputs and authority text."""
        if role == "docs":
            cwd = self.docs_inputs()
            inputs = sorted(cwd.glob("*.md"))
            extra = (
                "Measure relevance of subagent documentation and handovers only. Read no code, "
                "source diffs, source checkout or logs. Use only supplied Markdown files. "
                "Resolve only findings that the issue register marks with role=docs. Never list "
                "a code- or system-owned issue in resolved, and never treat a worker's statement "
                "that code findings are fixed as authority to resolve them. Code implementation, "
                "acceptance and platform evidence remain code-owned."
            )
        else:
            cwd = Path(candidate.path)
            inputs = [self.plan / self.manifest.reviewers.code, self.plan / "requirement.md",
                      self.plan / "plan.md", self.run_dir / "issues.md"]
            inputs += [Path(self.state.workers[w.id]["handover"]) for w in self.manifest.workers]
            extra = (
                "Review integration implementation against requirements and acceptance criteria. "
                "Resolve only findings that the issue register marks with role=code. Never resolve "
                "docs- or system-owned issues. If an acceptance criterion needs evidence this "
                "environment cannot produce (for example native Windows ACL, reparse-point or "
                "locking behaviour), use action: escalate and name the exact human prerequisite "
                "instead of requesting another same-platform repair. Never claim an unverified "
                "platform passed."
            )
        return cwd, inputs, extra

    def _review_evidence_name(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.run_dir.resolve()))
        except (ValueError, OSError):
            return str(path.resolve())

    def review_evidence_digest(self, role, candidate, inputs) -> str:
        """Stable digest of the role's immutable review evidence.

        The rendered issue register is deliberately excluded: it is a routing
        projection that a role's own acceptance mutates, so including it would
        make every accepted result self-invalidating on resume.
        """
        entries = []
        for path in inputs:
            if path.name == "issues.md" or path.stem.endswith("-issues"):
                continue
            entries.append((self._review_evidence_name(path), _file_sha256(path)))
        reviewer = within(self.plan, self.manifest.reviewers.code if role == "code"
                          else self.manifest.reviewers.docs)
        payload = {
            "role": role,
            "candidate_sha": candidate.sha,
            "approved_hashes": sorted(self.state.approved_hashes.items()),
            "reviewer_instruction": _file_sha256(reviewer),
            "inputs": sorted(entries),
        }
        return _sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False))

    def resume_review_applications(self) -> None:
        """Resume durable applications before any relevant dispatch."""
        self.state = resume_review_applications(
            self.store, self.state, self.manifest)

    def dependencies(self) -> dict:
        return load_validated_dependencies(self.manifest, self.plan, self.state)

    def dispatchable_issues(self, worker, dependencies: dict | None = None,
                            active_resources: set[str] | None = None):
        """Ready issues for one worker, with dependency/resource waits named."""
        dependencies = self.dependencies() if dependencies is None else dependencies
        candidates = [issue for issue in self.state.issues.values()
                      if issue.worker == worker.id
                      and issue.lifecycle in _DISPATCHABLE]
        return select_dispatchable_issues(
            candidates, self.state, dependencies, active_resources)

    def record_human_disposition(self, disposition: HumanDisposition) -> None:
        """Apply one explicitly recorded human control action.

        This is a contract-layer surface, not a new CLI command. It refuses an
        unknown issue and records the guarded transition as audit evidence.
        The durable operation intent and the transition are saved before the
        single audit append, so an interruption at that boundary is detected
        as an audit gap and can be reconciled without repeating the transition.
        """
        issue = self.state.issues.get(disposition.issue_id)
        if issue is None:
            raise ContractError(
                f"Human disposition names unknown issue {disposition.issue_id}")
        prior_digest = _accounting_digest(issue)
        transition = apply_human_disposition(issue, disposition)
        accounting_digest = _accounting_digest(issue)
        if accounting_digest != prior_digest:
            raise ContractError(
                f"Human disposition for {issue.id} changed counters or assignments; "
                "refusing to save it")
        key = _human_disposition_operation_key(issue.id, transition)
        self.state = self.store.record_operation(self.state, key, {
            "status": "intent",
            "issue": issue.id,
            "disposition_kind": disposition.kind,
            "scope": disposition.scope,
            "evidence": list(disposition.evidence),
            "allowance_disposition": disposition.allowance_disposition,
            "dependencies": list(disposition.dependencies),
            "deferred_stage": disposition.deferred_stage,
            "from_status": transition.from_status,
            "to_status": transition.to_status,
            "transition_recorded_at": transition.recorded_at,
            "transition_guard": transition.guard,
            "transition_evidence": list(transition.evidence),
            "prior_accounting_digest": prior_digest,
            "accounting_digest": accounting_digest,
        })
        event = self.event(
            "human_disposition", issue=issue.id,
            disposition_kind=disposition.kind,
            from_status=transition.from_status, to_status=transition.to_status,
            scope=disposition.scope, evidence=transition.evidence,
            deferred_stage=disposition.deferred_stage,
            operation=key, transition_recorded_at=transition.recorded_at,
        )
        self.state = self.store.record_operation(self.state, key, {
            "status": "completed",
            "audit_event_id": event["id"],
            "reconciled": False,
        })

    def reconcile_human_disposition(self, disposition: HumanDisposition, *,
                                    expected_accounting_digest: str | None = None
                                    ) -> dict:
        """Append only the missing audit for an already-saved disposition."""
        self.state, event = reconcile_human_disposition(
            self.store, self.state, disposition,
            expected_accounting_digest=expected_accounting_digest)
        return event

    def finalize_review_transactions(self, candidate) -> None:
        """Complete or refuse role transactions recorded before an interruption."""
        for key, operation in list(self.state.operations.items()):
            if not key.startswith(_REVIEW_RESULT_PREFIX):
                continue
            if not isinstance(operation, dict):
                raise Escalation(f"Review transaction {key} is malformed; refusing resume")
            recorded_sha = operation.get("candidate_sha")
            if recorded_sha != candidate.sha:
                if operation.get("status") == "completed":
                    continue  # historical evidence for an earlier immutable candidate
                raise Escalation(
                    f"Review transaction {key} belongs to candidate {recorded_sha}, "
                    "not the current candidate; refusing to apply it"
                )
            if operation.get("status") == "intent":
                self.state, result = _finish_review_operation(
                    self.store, self.state, key, operation, self.manifest
                )
                self.event("review_transaction_replayed", transaction=key,
                           role=result.role, result=result.result)
        self.resume_review_applications()
        gaps = review_transaction_gaps(self.state, self.store.events(self.state.run_id))
        if gaps:
            raise Escalation(
                "Review role transactions are incomplete or unaudited: "
                + "; ".join(gaps)
            )

    def transition(self, *, phase=None, stage=None):
        if phase:
            self.state.phase = phase
        if stage:
            self.state.stage = stage
        try:
            self.save()
        except ContractError as exc:
            if "Stale state revision" not in str(exc):
                raise
            # A concurrent durable commit advanced the revision. The disk state
            # is authoritative; reapply only the requested transition.
            error = self.state.error
            self.state = self.store.load(self.state.run_id)
            if phase:
                self.state.phase = phase
            if stage:
                self.state.stage = stage
            if error is not None:
                self.state.error = error
            self.save()
        self.event("transition", phase=self.state.phase, stage=self.state.stage, cycle=self.state.round)

    def _boundary_feature(self) -> FeatureSpec:
        """Read the current effective settings without hot-swapping the call.

        The loader is supplied by the CLI and reuses the same selected files,
        global-model policy and manifest limits. It deliberately cannot prompt;
        a changed or newly unconsented fallback raises before dispatch.
        """
        if self.settings_loader is None:
            return self.feature
        refreshed, _ = self.settings_loader()
        refreshed.limits = self.manifest.limits
        return refreshed

    def check_approval(self):
        # Compare fresh boundary settings against approval, but keep this
        # invocation's resolved feature (and any active request) unchanged.
        actual = approval_hashes(self.plan, self.manifest, self._boundary_feature())
        if actual != self.state.approved_hashes:
            self.state.error = "Approved inputs changed; run orch approve before continuing"
            self.state.previous_phase = self.state.phase
            self.transition(phase="PLAN_READY")
            self.event("approval_invalidated")
            raise ApprovalRequired(self.state.error)

    def check_control(self):
        path = self.run_dir / "control.json"
        if not path.exists():
            return
        request = json.loads(path.read_text())["request"]
        path.unlink()
        self.state.previous_phase = self.state.phase
        self.transition(phase="ABORTED" if request == "abort" else "PAUSED")
        raise asyncio.CancelledError

    async def dispatch(self, role, logical_id, instruction, output, cwd, inputs=()):
        from orch.agents import AgentRequest, PreflightError, validate_launch_paths

        self.check_approval()
        info = self.state.workers.setdefault(logical_id, {})
        attempt = int(info.get("attempt", 0)) + 1
        attempt_id = f"{logical_id}-{self.state.round}-{attempt}"
        log_dir = self.run_dir / f"round-{self.state.round}" / "logs" / attempt_id
        model = self.feature.models.resolve(role)
        if info.get("uncertain_attempt_id"):
            raise Escalation(
                f"Prior dispatch {logical_id}/{info['uncertain_attempt_id']} has uncertain ownership"
            )
        # Validate every mandatory path before writing a durable launch intent.
        # A missing input is a deterministic configuration error, not an
        # uncertain spawn window, so it must never leave an intent that resume
        # cannot distinguish from a real launch.
        try:
            validate_launch_paths(instruction, tuple(inputs), output, log_dir)
        except PreflightError as exc:
            info.update(status="preflight_failed", role=role, attempt=attempt,
                        attempt_id=attempt_id, activity=timestamp(),
                        output=str(output), log=str(log_dir / "stdout.log"),
                        preflight_error={"kind": exc.input_kind, "path": exc.path,
                                         "message": str(exc)})
            self.save()
            self.event("dispatch_preflight_failed", agent=logical_id, attempt=attempt_id,
                       stage="before-intent", input_kind=exc.input_kind,
                       path=exc.path, reason=str(exc))
            raise Escalation(
                f"Dispatch {logical_id}/{attempt_id} rejected before launch "
                f"({exc.input_kind}): {exc}"
            ) from exc
        info.pop("process", None)
        info.pop("process_attempt_id", None)
        info.update(status="running", role=role, attempt=attempt, attempt_id=attempt_id,
                    activity=timestamp(),
                    started_at=timestamp(), output=str(output), log=str(log_dir / "stdout.log"))
        self.save()
        self.event("dispatch_intent", agent=logical_id, attempt=attempt_id, role=role,
                   model=model.model, instruction=str(instruction), output=str(output))
        request = AgentRequest(role=role, logical_id=logical_id, attempt_id=attempt_id,
                               instruction_file=instruction, output_file=output, cwd=cwd,
                               model=model, timeout_seconds=self.feature.limits.worker_timeout_min * 60,
                               input_files=tuple(inputs), log_dir=log_dir,
                               writable_roots=(cwd, output.parent))
        adapter = self.adapters["planner" if role == "planner" else "opencode"]
        def activity(event):
            info["activity"] = timestamp()
            # Process identities are journaled before waiting for agent completion.
            kind = event.get("kind")
            if kind == "invocation_identity":
                if info.get("attempt_id") == attempt_id:
                    recorded = record_invocation_identity(
                        info, logical_id, attempt_id, event)
                    if recorded is not None:
                        self.event(
                            "invocation_identity", agent=logical_id, attempt=attempt_id,
                            adapter=recorded["adapter"], provider=recorded["provider"],
                            model=recorded["model"],
                            evidence_status=recorded["evidence_status"])
            elif kind == "process_started":
                info["process"] = {k: event[k] for k in ("pid", "create_time") if k in event}
                info["process_attempt_id"] = attempt_id
            elif kind == "process_completed" and info.get("process_attempt_id") == attempt_id:
                info.pop("process", None)
                info.pop("process_attempt_id", None)
            if kind in {"process_started", "process_children", "process_completed"}:
                self.event(kind, agent=logical_id, attempt=attempt_id,
                           **{k: v for k, v in event.items() if k != "kind"})
            self.save()
        # Recheck after persisting the intent and immediately before the
        # adapter can create a process. Retries enter this boundary anew.
        try:
            self.check_approval()
        except ApprovalRequired:
            # This invocation was prevented before process creation, so recovery
            # must not mistake its intent-only journal for an uncertain spawn.
            info["status"] = "blocked"
            self.save()
            self.event("dispatch_blocked", agent=logical_id, attempt=attempt_id,
                       reason="approval changed before process launch")
            raise
        try:
            result = await adapter.run(request, on_event=activity)
        except asyncio.CancelledError:
            info["status"] = "interrupted"
            attempt_started = any(
                event.get("kind") == "process_started"
                and event.get("agent") == logical_id
                and event.get("attempt") == attempt_id
                for event in self.store.events(self.state.run_id)
            )
            if not attempt_started:
                info["uncertain_attempt_id"] = attempt_id
            self.save()
            self.event("agent_interruption_uncertain" if not attempt_started else "agent_interrupted",
                       agent=logical_id, attempt=attempt_id)
            self.check_approval()
            raise
        except Exception as exc:
            preflight = _preflight_failure(exc)
            if preflight is not None:
                # The adapter rejects unusable inputs before probing, building
                # a command or creating a process. That is a durable, proven
                # prelaunch failure: no ownership uncertainty exists to keep.
                info.update(status="preflight_failed", activity=timestamp(),
                            preflight_error={"kind": preflight.input_kind,
                                             "path": preflight.path,
                                             "message": str(preflight)})
                self.save()
                self.event("dispatch_preflight_failed", agent=logical_id, attempt=attempt_id,
                           stage="adapter", input_kind=preflight.input_kind,
                           path=preflight.path, reason=str(preflight))
                self.check_approval()
                raise Escalation(
                    f"Dispatch {logical_id}/{attempt_id} rejected before launch "
                    f"({preflight.input_kind}): {preflight}"
                ) from exc
            attempt_started = any(
                event.get("kind") == "process_started"
                and event.get("agent") == logical_id
                and event.get("attempt") == attempt_id
                for event in self.store.events(self.state.run_id)
            )
            if not attempt_started:
                info.update(status="uncertain", activity=timestamp(), uncertain_attempt_id=attempt_id)
                self.save()
                self.event("agent_launch_uncertain", agent=logical_id, attempt=attempt_id,
                           error=_failure_message(exc))
                self.check_approval()
                raise Escalation(
                    f"Dispatch {logical_id}/{attempt_id} failed without durable process identity; "
                    f"refusing a retry: {_failure_message(exc)}"
                ) from exc
            info.update(status="failed", activity=timestamp())
            self.save()
            self.event("agent_failed", agent=logical_id, attempt=attempt_id,
                       error=_failure_message(exc))
            self.check_approval()
            raise
        info.update(status="done" if result.exit_code == 0 else "failed", activity=timestamp(),
                    duration_seconds=result.duration_seconds)
        self.save()
        self.event("agent_completed", agent=logical_id, attempt=attempt_id,
                   exit_code=result.exit_code, duration_seconds=result.duration_seconds,
                   usage=result.usage, model=model.model, adapter_version=result.adapter_version)
        # A failed call is still an approval boundary: never retry using inputs
        # or runtime policy edited while the attempt was active.
        self.check_approval()
        if result.exit_code != 0:
            retries = info.get("infrastructure_retries", 0)
            if retries < self.feature.limits.max_infrastructure_retries:
                info["infrastructure_retries"] = retries + 1
                self.save()
                self.event("infrastructure_retry", agent=logical_id, retry=retries + 1)
                return await self.dispatch(role, logical_id, instruction, output, cwd, inputs)
            raise Escalation(f"Agent {logical_id} failed: {result.reason or result.exit_code}")
        info["infrastructure_retries"] = 0
        self.save()
        return result

    def input_file(self, name, title, files, extra=""):
        path = self.run_dir / f"round-{self.state.round}" / "inputs" / f"{name}.md"
        text = f"# {title}\n\n"
        if not name.startswith("review-docs"):
            text += f"Canonical plan folder: {self.plan}\n\n"
        text += "Read these files:\n\n" + "\n".join(f"- {p}" for p in files)
        text += "\n\n" + extra + "\n"
        atomic_write(path, text)
        return path

    async def checks(self, cwd, label, integration=False):
        for check in self.manifest.checks:
            if check.applies_to not in {"both", "integration" if integration else "worker"}:
                continue
            path = self.run_dir / f"round-{self.state.round}" / "checks" / label / check.name
            path.mkdir(parents=True, exist_ok=True)
            argv = check.argv or (["bash", "-lc", check.cmd] if check.shell == "bash" else
                    ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                     f"& {{ {check.cmd} }}; if ($LASTEXITCODE) {{ exit $LASTEXITCODE }}; if (-not $?) {{ exit 1 }}"])
            result = await self.runner.run(argv, cwd=cwd, timeout=check.timeout_seconds,
                                            log_paths=(path / "stdout.log", path / "stderr.log"))
            self.event("check_result", agent=label, check=check.name, exit_code=result.exit_code)
            if result.exit_code != 0:
                raise CheckError(f"Check {check.name} failed; read {path}")

    async def worker_check(self, worker, info):
        workspace = self.record.workers[worker.id]
        handover, body = read_markdown(Path(info["handover"]), Handover)
        if handover.agent != worker.id or handover.round != self.state.round or handover.status != "done":
            raise ContractError("Handover identity/round/status mismatch")
        if "## Interfaces and contracts" not in body:
            raise ContractError("Handover needs Interfaces and contracts section")
        changes = self.git.inspect_changes(workspace, allowed_paths=worker.allowed_paths)
        if changes.violations:
            raise ContractError(f"Scope violations: {changes.violations}")
        if set(handover.files_changed) != set(changes.paths):
            raise ContractError("Handover files_changed must match actual diff")
        if not set(handover.docs_changed) <= set(handover.files_changed):
            raise ContractError("docs_changed must be a subset of files_changed")
        await self.checks(workspace.path, worker.id)
        return changes

    def previous_handover_evidence(self, worker, info):
        """Return a previous attempt's real handover, never its intended output.

        ``workers.<id>.handover`` records where the last attempt was asked to
        write. It becomes optional evidence only when a validated handover
        exists there, stays inside this run's directory, and belongs to the
        same worker. Missing or invalid paths are journaled and omitted; no
        handover is ever fabricated to satisfy validation.
        """
        raw = info.get("handover")
        if not raw:
            return None
        path = Path(raw)

        def skip(reason):
            self.event("handover_evidence_skipped", agent=worker.id, path=str(raw), reason=reason)

        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return skip("path does not exist")
        if path.suffix.lower() != ".md" or path.is_symlink() or not resolved.is_file():
            return skip("not a regular Markdown file")
        if not resolved.is_relative_to(self.run_dir.resolve()):
            return skip("outside this run directory")
        try:
            handover, _ = read_markdown(resolved, Handover)
        except (ContractError, OSError) as exc:
            return skip(f"invalid handover: {exc}")
        if handover.agent != worker.id:
            return skip("handover identity does not match this worker")
        return resolved

    def _finish_system_issue(self, identity: str, evidence: list[str]) -> None:
        """Finish one workflow-owned check issue through the guarded path."""
        issue = self.state.issues.get(identity)
        if issue is None or issue.lifecycle == "finished":
            return
        if issue.lifecycle not in ("pending", "in_repair"):
            raise LifecycleTransitionError(
                f"Issue {identity} is {issue.lifecycle}; check evidence cannot finish it")
        transition = finish_system_issue(issue, check_evidence=evidence)
        self.event("lifecycle_transition", issue=identity,
                   from_status=transition.from_status, to_status=transition.to_status,
                   guard=transition.guard, evidence=transition.evidence)

    async def run_worker(self, worker, active_resources: set[str] | None = None):
        require_carry_forward_import(self.state, self.plan, self.manifest)
        self.check_recovery_audit()
        self.resume_review_applications()
        workspace = self.record.workers[worker.id]
        info = self.state.workers.setdefault(worker.id, {})
        if info.get("commit_pending"):
            from orch.git_ops import git, head
            pending = info["commit_pending"]
            current = head(workspace.path)
            if current != pending["baseline"]:
                if git(workspace.path, "rev-parse", "HEAD^").strip() != pending["baseline"]:
                    raise Escalation("Interrupted worker commit has unexpected ancestry")
                actual = self.git.inspect_changes(workspace, baseline_sha=pending["baseline"], allowed_paths=worker.allowed_paths)
                if (actual.violations or actual.paths != pending.get("paths", actual.paths)
                        or _change_snapshot(workspace.path, actual.paths) != pending.get("snapshot")):
                    raise Escalation("Interrupted worker commit does not match validated changes")
                workspace.baseline_sha = current
                info.update(sha=current, checked=True, status="done", checks="pass")
                info.pop("commit_pending")
                self.state.workspace = {**self.state.workspace, **self.record.to_dict()}
                self.save()
                self.state = self.store.record_operation(
                    self.state, f"worker-commit:{worker.id}:{pending['baseline']}",
                    {"status": "completed", "sha": current, "reconciled": True},
                )
                self.event("worker_commit_reconciled", agent=worker.id, sha=current)
        if info.get("checked") and info.get("status") == "done":
            return
        dependencies = [self.state.workers[d]["sha"] for d in worker.depends_on]
        if dependencies and info.get("dependency_shas") != dependencies:
            try:
                self.git.import_dependencies(workspace, dependencies)
            except __import__("orch.git_ops", fromlist=["GitConflict"]).GitConflict as conflict:
                await self.resolve_conflict(conflict)
                self.git.import_dependencies(workspace, dependencies)
            info["dependency_shas"] = dependencies
            self.state.workspace = {**self.state.workspace, **self.record.to_dict()}
            self.save()
        dependencies_view = self.dependencies()
        while True:
            self.check_control()
            self.check_approval()
            candidates = [i for i in self.state.issues.values()
                          if i.worker == worker.id and i.lifecycle in _DISPATCHABLE]
            if not candidates:
                # A worker with no registered issue still performs its owned
                # work pass (initial work, resume after a failed attempt, or a
                # rework handover); it must not remain permanently unchecked.
                issues, waiting = [], {}
            else:
                issues, waiting = select_dispatchable_issues(
                    candidates, self.state, dependencies_view, active_resources)
                if not issues:
                    self.event("dispatch_waiting", agent=worker.id,
                               waits={identity: reasons
                                      for identity, reasons in waiting.items()})
                    return
            attempt = int(info.get("attempt", 0)) + 1
            output = self.run_dir / f"round-{self.state.round}" / "handover" / f"{worker.id}-{attempt}.md"
            inputs = [within(self.plan, worker.instruction)]
            previous = self.previous_handover_evidence(worker, info)
            if previous is not None:
                inputs.append(previous)
            if info.get("feedback"):
                inputs.append(Path(info["feedback"]))
            inputs.append(self.run_dir / "issues.md")
            wrapper = self.input_file(f"worker-{worker.id}-{attempt}", worker.title, inputs,
                "Inspect existing worktree and partial work first; continue rather than restart. "
                "Do not commit, change branches, or edit canonical plan files. "
                f"Write a valid handover as your last action to {output}. "
                "Include a code-free Interfaces and contracts section. "
                f"Allowed paths: {worker.allowed_paths}.")
            role = "replacement" if any(len(i.assignments) > 1 for i in issues) else "worker"
            for issue in issues:
                # Dispatch is a guarded lifecycle transition, recorded with the
                # durable intent. A waiting issue never reaches this point.
                if issue.lifecycle == "pending":
                    apply_lifecycle_transition(
                        issue, "in_repair",
                        TransitionGuard(
                            identity_valid=True, owner_lineage_valid=True,
                            dispatch_intent=True, dependencies_ready=True,
                            allowance_available=True, process_ownership_known=True,
                        ),
                        evidence=[f"dispatch:{worker.id}:attempt-{attempt}"],
                    )
                else:
                    apply_lifecycle_transition(
                        issue, "in_repair",
                        TransitionGuard(subsequent_attempt=True),
                        evidence=[f"dispatch:{worker.id}:attempt-{attempt}"],
                    )
                issue.assignments[-1].repairs += 1
                issue.attempt = attempt
                self.state.activity.append(_dispatch_activity(
                    issue, worker.id, attempt))
            info.update(handover=str(output), checked=False, branch=workspace.branch,
                        worktree=str(workspace.path))
            self.save()
            await self.dispatch(role, worker.id, wrapper, output, workspace.path, inputs)
            # dispatch/save copies state, so recover the authoritative nested dictionary.
            info = self.state.workers[worker.id]
            try:
                changes = await self.worker_check(worker, info)
                info["commit_pending"] = {"baseline": workspace.baseline_sha,
                                          "fingerprint": changes.fingerprint,
                                          "paths": changes.paths,
                                          "snapshot": _change_snapshot(workspace.path, changes.paths)}
                self.save()
                operation_key = f"worker-commit:{worker.id}:{workspace.baseline_sha}"
                self.state = self.store.record_operation(
                    self.state, operation_key,
                    {"status": "intent", "baseline": workspace.baseline_sha,
                     "fingerprint": changes.fingerprint, "attempt": attempt},
                )
                info = self.state.workers[worker.id]
                sha = self.git.commit_worker(workspace, changes)
                self.state = self.store.record_operation(
                    self.state, operation_key,
                    {"status": "completed", "sha": sha},
                )
                info = self.state.workers[worker.id]
                info.pop("commit_pending", None)
                workspace.baseline_sha = sha
                self._finish_system_issue(
                    f"check-{worker.id}",
                    [f"worker-check:{worker.id}:round-{self.state.round}:pass"])
                self._finish_system_issue(
                    f"post-check-{worker.id}",
                    [f"post-check:{worker.id}:round-{self.state.round}:pass"])
                info.update(sha=sha, checked=True, status="done", checks="pass")
                self.state.workspace = {**self.state.workspace, **self.record.to_dict()}
                self.save()
                return
            except (ContractError, CheckError, OSError) as exc:
                self.event("worker_check_failed", agent=worker.id, error=str(exc))
                key = f"check-{worker.id}"
                if key not in self.state.issues:
                    self.state.issues[key] = create_system_issue(
                        key, worker.id, root_issue_id=key, summary=str(exc),
                        evidence=[f"worker-check-failure:{worker.id}:round-{self.state.round}"],
                    )
                issue = self.state.issues[key]
                current = issue.assignments[-1]
                if current.repairs and current.repairs not in (current.failed_repair_ordinals or []):
                    current.failed_repairs += 1
                    if current.failed_repair_ordinals is None:
                        current.failed_repair_ordinals = []
                    current.failed_repair_ordinals.append(current.repairs)
                if current.failed_repairs >= self.manifest.limits.max_failed_repairs_per_assignment:
                    if len(issue.assignments) - 1 >= self.manifest.limits.max_reassignments:
                        raise Escalation(f"Check repair exhausted for {worker.id}") from exc
                    issue.assignments.append(AssignmentState(failed_repair_ordinals=[]))
                feedback = self.run_dir / f"round-{self.state.round}" / "feedback" / f"{worker.id}-{attempt}.md"
                atomic_write(feedback, f"# CHECK feedback\n\n{exc}\n")
                info["feedback"] = str(feedback)
                self.save()

    async def work(self):
        # The approved carry-forward import is a dispatch precondition: until it
        # is completed with the matching digest, no wave may record an attempt.
        require_carry_forward_import(self.state, self.plan, self.manifest)
        # Finish durable review applications before any repair dispatch: an
        # interrupted acceptance must not be silently skipped by new work.
        self.resume_review_applications()
        pending = {w.id: w for w in self.manifest.workers if not self.state.workers.get(w.id, {}).get("checked")}
        while pending:
            ready = [w for w in pending.values() if all(self.state.workers.get(d, {}).get("checked") for d in w.depends_on)]
            if not ready:
                raise Escalation("No schedulable workers; dependency state is inconsistent")
            dependencies = self.dependencies()
            # Serialise declared shared resources across workers by scheduling a
            # wave whose selected issues do not overlap. Waiting issues keep
            # their status and consume no attempt.
            wave: list = []
            used: set[str] = set()
            wave_resources: dict[str, set[str]] = {}
            for worker in ready:
                candidates = [issue for issue in self.state.issues.values()
                              if issue.worker == worker.id
                              and issue.lifecycle in _DISPATCHABLE]
                selected, _waiting = select_dispatchable_issues(
                    candidates, self.state, dependencies, used)
                resources = set()
                for issue in selected:
                    dependency = dependencies.get(issue.id)
                    if dependency:
                        resources.update(dependency.shared_resource_paths)
                if candidates and not selected:
                    continue
                wave.append(worker)
                wave_resources[worker.id] = resources
                used.update(resources)
            if not wave:
                return
            before = {identity: self.state.workers.get(identity, {}).get("attempt", 0)
                      for identity in pending}
            semaphore = asyncio.Semaphore(self.manifest.limits.concurrency)
            async def one(worker, semaphore=semaphore, resources=wave_resources):
                async with semaphore:
                    await self.run_worker(worker, resources.get(worker.id, set()))
            async with asyncio.TaskGroup() as group:
                for worker in wave:
                    group.create_task(one(worker))
            for worker in wave:
                if self.state.workers.get(worker.id, {}).get("checked"):
                    del pending[worker.id]
            after = {identity: self.state.workers.get(identity, {}).get("attempt", 0)
                     for identity in pending}
            if pending and after == before:
                waits = []
                for worker in pending.values():
                    candidates = [issue for issue in self.state.issues.values()
                                  if issue.worker == worker.id
                                  and issue.lifecycle in _DISPATCHABLE]
                    _selected, waiting = select_dispatchable_issues(
                        candidates, self.state, dependencies)
                    waits.extend(f"{identity}: {', '.join(reasons)}"
                                 for identity, reasons in waiting.items())
                raise Escalation(
                    "No schedulable repair issue; dependency or shared-resource waits "
                    "are unresolved: " + ("; ".join(waits) or "no pending issue"))

    def invalidate_mutated_workers(self) -> bool:
        """Recheck worker snapshots before combining their commits.

        Dirty post-check changes are retained and returned to their owner. An
        unexpected branch-head change cannot be safely attributed and escalates.
        """
        from orch.git_ops import git, head

        changed: list[str] = []
        for worker in self.manifest.workers:
            info = self.state.workers.get(worker.id, {})
            workspace = self.record.workers[worker.id]
            if not info.get("checked") or info.get("status") != "done":
                raise Escalation(f"Worker {worker.id} is not checked before candidate assembly")
            expected = info.get("sha")
            actual = head(workspace.path)
            if not expected or actual != expected:
                raise Escalation(
                    f"Completed worker {worker.id} branch changed after CHECK "
                    f"(expected {expected or 'no recorded SHA'}, found {actual})"
                )
            if git(workspace.path, "status", "--porcelain").strip():
                changed.append(worker.id)
        if not changed:
            return False

        self.state.round += 1
        for worker_id in changed:
            info = self.state.workers[worker_id]
            info.update(checked=False, status="rework")
            issue_id = f"post-check-{worker_id}"
            if issue_id not in self.state.issues:
                self.state.issues[issue_id] = create_system_issue(
                    issue_id, worker_id, root_issue_id=issue_id,
                    summary="Worker worktree changed after its successful CHECK; "
                            "revalidate retained changes.",
                    evidence=[f"post-check-mutation:{worker_id}:round-{self.state.round}"],
                )
            feedback = self.run_dir / f"round-{self.state.round}" / "feedback" / f"{worker_id}.md"
            atomic_write(
                feedback,
                "# Post-CHECK mutation\n\nThe worktree changed after its successful check. "
                "Inspect and retain the changes, then produce a fresh handover and pass CHECK.\n",
            )
            info["feedback"] = str(feedback)
        self.event("post_check_mutation", agents=changed)
        self.transition(stage="WORK")
        return True

    async def resolve_conflict(self, conflict):
        details = conflict.record
        data = details.to_dict() if hasattr(details, "to_dict") else vars(details)
        target = Path(data.get("path") or data.get("worktree") or data.get("target_path"))
        output = self.run_dir / f"round-{self.state.round}" / "handover" / "conflict-resolver.md"
        instruction = self.input_file("conflict", "Resolve integration conflict", [self.plan / "plan.md"],
             "Resolve/cherry-pick the recorded source commits in the affected owned worktree. "
             "Do not change worker branches or main. If intent is uncertain, write blocked handover. "
             "Commit the completed resolution in this owned worktree. Explicitly account for each source commit SHA in your handover body.\n\n"
             + json.dumps(data, default=str, indent=2))
        await self.dispatch("conflict_resolver", "conflict-resolver", instruction, output, target)
        handover, _ = read_markdown(output, Handover)
        if handover.status != "done":
            raise Escalation("Conflict resolver could not determine intended result")
        _, body = read_markdown(output, Handover)
        if any(sha not in body for sha in details.source_commits):
            raise Escalation("Resolver handover must explicitly account for every source commit")
        allowed = sorted({path for worker in self.manifest.workers for path in worker.allowed_paths})
        self.git.accept_resolution(details, details.source_commits, allowed_paths=allowed)
        if self.git.record:
            self.record = self.git.record
            self.state.workspace = {**self.state.workspace, **self.record.to_dict()}
        self.save()
        self.event("conflict_resolved", handover=str(output))

    def docs_inputs(self):
        """Only selected Markdown context goes into the docs review directory."""
        destination = self.run_dir / f"round-{self.state.round}" / "docs-review"
        destination.mkdir(parents=True, exist_ok=True)
        context = [self.plan / "requirement.md", self.plan / "plan.md", self.plan / self.manifest.reviewers.docs, self.run_dir / "issues.md"]
        for worker in self.manifest.workers:
            info = self.state.workers[worker.id]
            context += [Path(info["handover"]), within(self.plan, worker.instruction)]
            handover, _ = read_markdown(Path(info["handover"]), Handover)
            context += [within(Path(self.state.candidate["path"]), p) for p in handover.docs_changed if p.endswith(".md")]
        for index, source in enumerate(context):
            text = source.read_text(encoding="utf-8")
            # Omit fenced snippets rather than supplying implementation excerpts to this role.
            text = re.sub(r"(?ms)^\s*```.*?^\s*```[^\n]*\n?", "[Code block omitted]\n", text)
            atomic_write(destination / f"{index:03d}-{source.name}", text)
        return destination

    async def review_role(self, role, candidate) -> ReviewRoleOutcome:
        """Run, validate and checkpoint exactly one independent reviewer role."""
        logical = "review-code" if role == "code" else "review-docs"
        cwd, inputs, base_extra = self.review_context(role, candidate)
        evidence_digest = self.review_evidence_digest(role, candidate, inputs)
        key = review_operation_key(role, candidate.sha, evidence_digest)

        operation = self.state.operations.get(key)
        if operation is not None:
            for commit_attempt in range(2):
                try:
                    async with self._review_commit_lock:
                        self.state = self.store.load(self.state.run_id)
                        operation = self.state.operations.get(key)
                        if operation is not None:
                            updated, result = _finish_review_operation(
                                self.store, self.state, key, operation, self.manifest
                            )
                            self._adopt_review_state(updated)
                    break
                except ContractError as exc:
                    if "Stale state revision" not in str(exc) or commit_attempt:
                        raise
            if operation is None:
                result = self.state.review_results[key]
            self._verify_review_result_evidence(result, candidate, evidence_digest)
            return ReviewRoleOutcome(role=role, result=result, reused=True)
        result = self.state.review_results.get(key)
        if result is not None:
            self._verify_review_result_evidence(result, candidate, evidence_digest)
            return ReviewRoleOutcome(role=role, result=result, reused=True)

        extra = (
            base_extra
            + f"\nCandidate SHA: {candidate.sha}. Write the strict {role} verdict to the "
              "instructed output path. "
              f"Include `reviewer: {role}` and `candidate_sha: {candidate.sha}` in the front "
              "matter. Preserve existing issue IDs; identify new issue parent/root IDs and "
              "explicitly list resolved IDs. List only your own role's issues in resolved."
        )
        base_attempt = int(self.state.workers.get(logical, {}).get("attempt", 0))
        for attempt in range(2):
            # Attempt-specific filenames preserve every rejected or repaired
            # verdict; a later independent retry never overwrites evidence that
            # an audited recovery or acceptance still references.
            output = (self.run_dir / f"round-{self.state.round}"
                      / f"verdict-{role}-{base_attempt + attempt + 1}.md")
            attempt_extra = extra + f"\nWrite the verdict to {output}."
            instruction = self.input_file(f"{logical}-{attempt}", "Review candidate", inputs,
                                          attempt_extra)
            await self.dispatch("code_reviewer" if role == "code" else "docs_reviewer", logical,
                                instruction, output, cwd, inputs)
            try:
                verdict, _ = read_markdown(output, Verdict)
                if verdict.reviewer != role:
                    raise ContractError("Wrong reviewer identity")
                if verdict.result == "escalate":
                    self.event("review_role_escalated", role=role,
                               attempt_id=self.state.workers.get(logical, {}).get("attempt_id"),
                               verdict=str(output))
                    return ReviewRoleOutcome(
                        role=role,
                        error=f"{role} reviewer requested escalation",
                    )
                for commit_attempt in range(2):
                    try:
                        async with self._review_commit_lock:
                            self.state = self.store.load(self.state.run_id)
                            updated, result = accept_review_result(
                                self.store, self.state, role=role,
                                candidate_sha=candidate.sha,
                                evidence_digest=evidence_digest,
                                attempt_id=str(
                                    self.state.workers[logical].get("attempt_id")),
                                verdict=verdict, verdict_path=output,
                                manifest=self.manifest,
                            )
                            self._adopt_review_state(updated)
                        break
                    except ContractError as exc:
                        # A concurrent independent role may have advanced the
                        # durable revision between dispatch and this synchronous
                        # commit. Replay is idempotent from the recorded intent.
                        if "Stale state revision" not in str(exc) or commit_attempt:
                            raise
                if result.escalation is None:
                    self.event("review_role_completed", role=role, result=result.result,
                               transaction=result.transaction,
                               attempt_id=result.attempt_id)
                return ReviewRoleOutcome(role=role, result=result)
            except (ContractError, OSError) as exc:
                attempt_id = self.state.workers.get(logical, {}).get("attempt_id")
                self.event("review_result_rejected", role=role, attempt_id=attempt_id,
                           reason=str(exc), verdict=str(output),
                           repairable=attempt == 0)
                if attempt:
                    return ReviewRoleOutcome(role=role, error=str(exc))
                extra = (
                    base_extra
                    + f"\nCandidate SHA: {candidate.sha}. Include `reviewer: {role}` and "
                      f"`candidate_sha: {candidate.sha}` in the front matter. Your previous verdict "
                      f"was invalid: {exc}. Repair it once. Resolve only findings you own; do not "
                      "resolve the other role's findings."
                )
        raise AssertionError("unreachable")

    def _adopt_review_state(self, updated: RunState) -> None:
        """Adopt routing changes without replacing an in-flight dispatch's state.

        Another independent reviewer may still be waiting on its process while
        this role commits. Replacing ``self.state`` wholesale would detach the
        nested worker dictionary that its dispatch callbacks update; this keeps
        the object identity for unrelated fields while taking the committed
        issue/transaction revision forward.
        """
        current = self.state
        current.issues = updated.issues
        current.review_results = updated.review_results
        current.review_applications = updated.review_applications
        current.activity = updated.activity
        current.dependency_record = updated.dependency_record
        current.operations = updated.operations
        current.revision = updated.revision
        current.updated_at = updated.updated_at

    def _verify_review_result_evidence(self, result: ReviewResult, candidate, evidence_digest):
        if result.candidate_sha != candidate.sha:
            raise ContractError(
                f"Accepted {result.role} result is bound to candidate {result.candidate_sha}, "
                f"not {candidate.sha}"
            )
        if result.evidence_digest != evidence_digest:
            raise ContractError(f"Accepted {result.role} result has different input evidence")
        path = Path(result.verdict_path)
        if not path.is_file() or _file_sha256(path) != result.verdict_sha256:
            raise ContractError(
                f"Accepted {result.role} verdict evidence changed or is missing: {path}"
            )

    async def stage(self):
        from orch.git_ops import Candidate, GitConflict
        self.check_approval()
        self.check_control()
        stage = self.state.stage
        if stage == "PREPARE":
            for role in ("worker", "code_reviewer", "docs_reviewer", "conflict_resolver"):
                self.feature.models.resolve(role)
            adapter = self.adapters["opencode"]
            if hasattr(adapter, "capabilities"):
                await asyncio.to_thread(adapter.capabilities)
        if stage == "PREPARE":
            self.git.prepare_workers(self.record, self.manifest)
            self.state.workspace = {**self.state.workspace, **self.record.to_dict()}
            self.transition(phase="RUNNING", stage="WORK")
        elif stage == "WORK":
            await self.work()
            self.transition(stage="CHECK")
        elif stage == "CHECK":
            if self.invalidate_mutated_workers():
                return
            shas = {w.id: self.state.workers[w.id]["sha"] for w in self.manifest.workers}
            try:
                candidate = self.git.build_candidate(shas, self.state.workspace.get("feature_head", self.record.base_sha), cycle=self.state.round)
            except GitConflict as conflict:
                await self.resolve_conflict(conflict)
                candidate = self.git.build_candidate(shas, self.state.workspace.get("feature_head", self.record.base_sha), cycle=self.state.round)
            try:
                await self.checks(candidate.path, "integration", integration=True)
            except (ContractError, CheckError) as exc:
                # A deterministic integration failure cannot safely be attributed to one owner.
                # Send identical evidence to each worker without pretending to judge its cause.
                for worker in self.manifest.workers:
                    key = f"integration-check-{worker.id}"
                    issue = self.state.issues.get(key)
                    if issue is None:
                        issue = create_system_issue(
                            key, worker.id, root_issue_id=key, summary=str(exc),
                            evidence=[(f"integration-check-failure:{worker.id}:"
                                       f"round-{self.state.round}")],
                        )
                        self.state.issues[key] = issue
                    else:
                        current = issue.assignments[-1]
                        if (current.repairs
                                and current.repairs not in (current.failed_repair_ordinals or [])):
                            current.failed_repairs += 1
                            if current.failed_repair_ordinals is None:
                                current.failed_repair_ordinals = []
                            current.failed_repair_ordinals.append(current.repairs)
                        if current.failed_repairs >= self.manifest.limits.max_failed_repairs_per_assignment:
                            if len(issue.assignments) - 1 >= self.manifest.limits.max_reassignments:
                                raise Escalation("Integration check repairs exhausted") from exc
                            issue.assignments.append(AssignmentState(failed_repair_ordinals=[]))
                    info = self.state.workers[worker.id]
                    info.update(checked=False, status="rework")
                    feedback = self.run_dir / f"round-{self.state.round}" / "feedback" / f"integration-{worker.id}.md"
                    atomic_write(feedback, f"# Integration CHECK failure\n\n{exc}\n\nInspect your owned changes and handovers; do not edit another worker's scope.\n")
                    info["feedback"] = str(feedback)
                self.event("integration_check_rework", error=str(exc))
                self.state.round += 1
                self.transition(stage="WORK")
                return
            for issue in list(self.state.issues.values()):
                if issue.id.startswith("integration-check-"):
                    self._finish_system_issue(
                        issue.id,
                        [(f"integration-check:{issue.worker}:"
                          f"round-{self.state.round}:pass")])
            self.state.candidate = candidate.to_dict() if hasattr(candidate, "to_dict") else {
                "sha": candidate.sha, "path": str(candidate.path), "source_shas": candidate.source_shas, "branch": candidate.branch}
            self.transition(stage="REVIEW")
        elif stage == "REVIEW":
            self.check_approval()
            self.settle_issue_ownership()
            candidate = Candidate(**{**self.state.candidate, "path": Path(self.state.candidate["path"])})
            self.verify_candidate(candidate)
            self.finalize_review_transactions(candidate)
            roles = ("code", "docs")
            if self.manifest.limits.concurrency > 1:
                outcomes = await asyncio.gather(
                    *(self.review_role(role, candidate) for role in roles),
                    return_exceptions=True,
                )
            else:
                outcomes = []
                for role in roles:
                    try:
                        outcomes.append(await self.review_role(role, candidate))
                    except (ContractError, OSError, RuntimeError) as exc:
                        # Keep the other independent role runnable; approval and
                        # cancellation boundaries still propagate unwrapped.
                        outcomes.append(exc)
            self.verify_candidate(candidate)
            accepted: dict[str, ReviewResult] = {}
            failures: list[str] = []
            for role, outcome in zip(roles, outcomes):
                if isinstance(outcome, BaseException):
                    failures.append(f"{role}: {_failure_message(outcome)}")
                elif outcome.error:
                    failures.append(f"{role}: {outcome.error}")
                elif outcome.result is not None:
                    accepted[role] = outcome.result
                else:
                    failures.append(f"{role}: no accepted role result")
            escalations = [f"{result.role}: {result.escalation}"
                           for result in accepted.values() if result.escalation]
            if failures:
                raise Escalation(
                    "Independent review role failure: " + "; ".join(failures))
            if escalations:
                # An issue-scoped stop is applied and processing continues. It
                # blocks the affected issue and promotion, not the whole run.
                self.event("review_issue_scoped_escalations",
                           escalations=escalations,
                           transactions=sorted(result.transaction
                                               for result in accepted.values()))
            if set(accepted) != set(roles):
                raise Escalation("Promotion requires one accepted result from each independent role")
            for result in accepted.values():
                if result.candidate_sha != candidate.sha:
                    raise Escalation(
                        f"{result.role} result is bound to a different candidate; refusing promotion"
                    )
            rework = [result for result in accepted.values() if result.result == "rework"]
            if rework:
                affected = {worker for result in rework for worker in result.affected_workers}
                while True:
                    expanded = affected | {w.id for w in self.manifest.workers
                                           if set(w.depends_on) & affected}
                    if expanded == affected:
                        break
                    affected = expanded
                for worker in affected:
                    info = self.state.workers[worker]
                    info["checked"] = False
                    info["status"] = "rework"
                    feedback = self.run_dir / f"round-{self.state.round}" / "feedback" / f"{worker}.md"
                    atomic_write(feedback, build_rework_feedback(
                        worker, self.manifest, affected, rework))
                    info["feedback"] = str(feedback)
                self.event("rework_issued", agents=sorted(affected),
                           transactions=[result.transaction for result in rework])
                self.state.round += 1
                self.transition(stage="WORK")
            else:
                blockers = promotion_blockers(
                    self.state, self.store.events(self.state.run_id))
                if blockers:
                    self.event("promotion_blocked", blockers=blockers)
                    raise Escalation(
                        "Promotion blocked by unresolved issue, dependency or "
                        "application evidence: " + "; ".join(blockers)
                    )
                self.event("dual_role_review_passed", candidate_sha=candidate.sha,
                           transactions=sorted(result.transaction for result in accepted.values()))
                self.transition(stage="MERGE")
        elif stage == "MERGE":
            candidate = Candidate(**{**self.state.candidate, "path": Path(self.state.candidate["path"])})
            from orch.git_ops import git, head
            if head(candidate.path) != candidate.sha or git(candidate.path, "status", "--porcelain").strip():
                raise Escalation("Candidate changed after review")
            self.git.promote_candidate(candidate, self.state.workspace.get("feature_head", self.record.base_sha))
            self.transition(stage="NOTIFY")
        elif stage == "NOTIFY":
            await self.finish_report("AWAITING_TEST")
            self.transition(phase="AWAITING_TEST")
            self.event("awaiting_human_test", report=str(self.plan / "report.md"))
        else:
            raise ContractError(f"Unknown stage: {stage}")

    async def finish_report(self, phase):
        from orch.platform_support import notify
        report_state = self.state.model_copy(update={"phase": phase})
        if self.feature.commit_run_history:
            destination = self.plan / "run-history" / self.state.run_id
            for source in self.run_dir.glob("round-*/*.md"):
                relative = source.relative_to(self.run_dir)
                atomic_write(destination / relative, source.read_text(encoding="utf-8"))
            for source in self.run_dir.glob("round-*/handover/*.md"):
                atomic_write(destination / source.relative_to(self.run_dir), source.read_text(encoding="utf-8"))
        write_report(report_state, self.store.events(self.state.run_id), self.run_dir, self.plan)
        if self.feature.commit_plan:
            from orch.git_ops import head

            self.git.commit_plan(self.plan, message=f"orch: {phase.lower()} report")
            # The report commit advances the owned feature branch. Persist the
            # new head so a later resume builds and promotes from actual
            # history instead of failing the reviewed-candidate head check.
            self.state.workspace["feature_head"] = head(self.record.feature_path)
            self.save()
        url = os.environ.get(self.config.webhook_env) if self.config.webhook_env else None
        result = await asyncio.to_thread(notify, f"Orchestrator: {self.state.slug}",
                                         f"{phase}; report: {self.plan / 'report.md'}", url)
        self.event("notification", success=result.success, detail=result.message)

    async def _monitor(self):
        while True:
            await asyncio.sleep(0.2)
            self.check_control()
            elapsed = (datetime.now(UTC) - datetime.fromisoformat(self.state.started_at)).total_seconds()
            if elapsed >= self.feature.limits.max_runtime_min * 60:
                raise Escalation("Total runtime limit reached")

    def recovery_audit_gaps(self) -> list[str]:
        """List unfinished or unaudited recovery records that block resume."""
        gaps: set[str] = set()
        events = self.store.events(self.state.run_id)
        for key, operation in self.state.operations.items():
            if not key.startswith(_RECOVERY_OPERATION_PREFIX):
                continue
            if not isinstance(operation, dict):
                gaps.add(f"{key} (malformed recovery operation)")
                continue
            attempt_id = key.removeprefix(_RECOVERY_OPERATION_PREFIX)
            status = operation.get("status")
            if status != "completed":
                gaps.add(f"{attempt_id} (resolution status {status or 'unknown'})")
                continue
            event_id = operation.get("event_id")
            matching = [event for event in events
                        if event.get("id") == event_id
                        and event.get("kind") == "uncertain_attempt_resolved"
                        and event.get("agent") == operation.get("worker")
                        and event.get("attempt") == attempt_id
                        and event.get("operator") == operation.get("operator")
                        and event.get("reason") == operation.get("reason")
                        and sorted(event.get("evidence_event_ids") or [])
                        == sorted(operation.get("evidence_event_ids") or [])]
            if len(matching) != 1:
                gaps.add(f"{attempt_id} (resolution audit event missing or conflicting)")
            for info in self.state.workers.values():
                if (info.get("attempt_id") == attempt_id
                        and info.get("uncertain_attempt_id") == attempt_id):
                    gaps.add(f"{attempt_id} (uncertainty marker remains)")
        for name, info in self.state.workers.items():
            history = info.get("uncertain_attempt_resolutions", []) or []
            if not isinstance(history, list):
                gaps.add(f"{name} (malformed resolution history)")
                continue
            for entry in history:
                attempt_id = entry.get("attempt") if isinstance(entry, dict) else None
                if not isinstance(attempt_id, str):
                    gaps.add(f"{name} (malformed resolution history)")
                    continue
                operation = self.state.operations.get(
                    f"{_RECOVERY_OPERATION_PREFIX}{attempt_id}") or {}
                if operation.get("status") != "completed":
                    gaps.add(f"{attempt_id} (resolution history is not audited)")
        for key, operation in self.state.operations.items():
            if not key.startswith(_REVIEW_BATCH_RECOVERY_PREFIX):
                continue
            if not isinstance(operation, dict) or operation.get("status") != "completed":
                gaps.add(f"{key} (incomplete review batch recovery)")
                continue
            event_id = operation.get("event_id")
            audit = [event for event in events
                     if event.get("id") == event_id
                     and event.get("kind") == "review_batch_recovered"
                     and event.get("transaction") == key]
            if len(audit) != 1:
                gaps.add(f"{key} (review batch recovery audit event missing or conflicting)")
            accepted = operation.get("accepted_transaction")
            if not isinstance(accepted, str) or accepted not in self.state.review_results:
                gaps.add(f"{key} (accepted role result missing)")
        for key, operation in self.state.operations.items():
            if not key.startswith(_HUMAN_DISPOSITION_OPERATION_PREFIX):
                continue
            if not isinstance(operation, dict):
                gaps.add(f"{key} (malformed human disposition operation)")
                continue
            if operation.get("status") != "completed":
                gaps.add(
                    f"{key} (human disposition audit "
                    f"{operation.get('status') or 'unknown'})")
                continue
            event_id = operation.get("audit_event_id")
            matching = [event for event in events
                        if event.get("id") == event_id
                        and event.get("kind") == "human_disposition"
                        and event.get("issue") == operation.get("issue")
                        and event.get("operation") == key
                        and event.get("transition_recorded_at")
                        == operation.get("transition_recorded_at")]
            if len(matching) != 1:
                gaps.add(
                    f"{key} (human disposition audit event missing or conflicting)")
        return sorted(gaps)

    def check_recovery_audit(self):
        gaps = self.recovery_audit_gaps()
        if gaps:
            raise Escalation(
                "Audited recovery is incomplete or unaudited for "
                + ", ".join(gaps)
                + "; re-run the audited recovery helper for that attempt before resume"
            )

    async def reconcile_processes(self):
        """Reconcile process evidence by attempt, failing closed on uncertain launch."""
        import psutil

        self.check_recovery_audit()

        from orch.processes import ProcessHandle, reconcile_events

        events = self.store.events(self.state.run_id)

        def attempt_events(agent, attempt):
            return [event for event in events
                    if event.get("agent") == agent and event.get("attempt") == attempt]

        # Close every process-start record that lacks its matching completion.
        # The event's attempt identity is part of the ownership proof: PID-only
        # matching could otherwise mistake an earlier call for a current one.
        starts = [event for event in events if event.get("kind") == "process_started"]
        for started in starts:
            agent, attempt = started.get("agent"), started.get("attempt")
            pid, create_time = started.get("pid"), started.get("create_time")
            if not isinstance(agent, str) or not isinstance(attempt, str):
                raise Escalation("Process start has no durable agent/attempt identity")
            if not isinstance(pid, int) or not isinstance(create_time, (int, float)) or create_time <= 0:
                raise Escalation(f"Process identity for {agent}/{attempt} is incomplete")
            correlated = attempt_events(agent, attempt)
            completed = any(
                event.get("kind") == "process_completed"
                and event.get("pid") == pid
                and event.get("create_time") == create_time
                for event in correlated
            )
            if completed:
                continue

            identities = reconcile_events([
                event for event in correlated
                if event.get("kind") in {"process_started", "process_children", "process_completed"}
            ])
            identity = next((item for item in identities
                             if item.get("pid") == pid and item.get("create_time") == create_time), None)
            if identity is None:
                raise Escalation(
                    f"Cannot reconcile process identity for {agent}/{attempt}; refusing a duplicate dispatch"
                )
            for live in identity["live"]:
                await self.runner.cancel(ProcessHandle(pid=live["pid"], create_time=live["create_time"]))
            for live in identity["live"]:
                try:
                    process = psutil.Process(live["pid"])
                    if (abs(process.create_time() - live["create_time"]) < 0.01
                            and process.status() != psutil.STATUS_ZOMBIE):
                        raise Escalation(
                            f"Owned process {live['pid']} for {agent}/{attempt} survived cancellation"
                        )
                except psutil.NoSuchProcess:
                    pass
            children_observed = any(
                event.get("kind") == "process_children" for event in correlated
            )
            if identity.get("status") == "reused":
                raise Escalation(
                    f"Process leader for {agent}/{attempt} was reused before ownership could be "
                    "reconciled; refusing a duplicate dispatch"
                )
            if identity.get("status") == "exited" and not children_observed:
                raise Escalation(
                    f"Process leader for {agent}/{attempt} exited and descendant ownership was not "
                    "recorded; refusing a duplicate dispatch"
                )
            self.event("process_completed", agent=agent, attempt=attempt, pid=pid,
                       create_time=create_time, reason="reconciled-after-interruption")
            events = self.store.events(self.state.run_id)

        for name, info in self.state.workers.items():
            if info.get("status") != "running" and not info.get("uncertain_attempt_id"):
                continue

            attempt = info.get("uncertain_attempt_id") or info.get("attempt_id")
            if not attempt:
                # Compatibility with state saved just before an intent. The
                # intent is written before adapter invocation, so no intent is
                # evidence that launch could not yet have happened.
                open_intents = []
                for event in events:
                    if event.get("kind") != "dispatch_intent" or event.get("agent") != name:
                        continue
                    event_attempt = event.get("attempt")
                    if not isinstance(event_attempt, str):
                        continue
                    related = attempt_events(name, event_attempt)
                    if not any(e.get("kind") in _ATTEMPT_TERMINAL_EVENTS for e in related):
                        open_intents.append(event_attempt)
                if open_intents:
                    attempt = open_intents[-1]
                    info["attempt_id"] = attempt

            related = attempt_events(name, attempt) if attempt else []
            intent_exists = any(event.get("kind") == "dispatch_intent" for event in related)
            process_started = any(event.get("kind") == "process_started" for event in related)
            terminal = any(event.get("kind") in _ATTEMPT_TERMINAL_EVENTS for event in related)
            if intent_exists and not process_started and not terminal:
                raise Escalation(
                    f"Dispatch {name}/{attempt} may have launched but has no durable process identity; "
                    "refusing to relaunch"
                )
            if info.get("process_attempt_id") != attempt:
                info.pop("process", None)
                info.pop("process_attempt_id", None)
            info["status"] = "interrupted"
            info.pop("process", None)
            info.pop("process_attempt_id", None)
            self.event("interrupted_job_reconciled", agent=name, attempt=attempt,
                       identity_established=process_started)
        self.save()

    async def run(self, one_stage=False):
        if self.state.phase not in {"APPROVED", "RUNNING"}:
            raise ContractError(f"Cannot execute from {self.state.phase}")
        if not self.state.started_at:
            self.state.started_at = timestamp()
            self.save()
        self.store.render(self.state)
        with self.store.lock("active"):
            stage_task = monitor = None
            try:
                await self.reconcile_processes()
                while self.state.phase in {"APPROVED", "RUNNING"}:
                    elapsed = (datetime.now(UTC) - datetime.fromisoformat(self.state.started_at)).total_seconds()
                    if elapsed >= self.feature.limits.max_runtime_min * 60:
                        raise Escalation("Total runtime limit reached")
                    stage_task, monitor = asyncio.create_task(self.stage()), asyncio.create_task(self._monitor())
                    done, _ = await asyncio.wait({stage_task, monitor}, return_when=asyncio.FIRST_COMPLETED)
                    if monitor in done:
                        stage_task.cancel()
                        await asyncio.gather(stage_task, return_exceptions=True)
                        await monitor
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                    await stage_task
                    if one_stage or self.feature.run_mode == "step":
                        break
            except ApprovalRequired:
                raise
            except asyncio.CancelledError:
                if self.state.phase not in {"PAUSED", "ABORTED"}:
                    self.state.previous_phase = self.state.phase
                    self.transition(phase="PAUSED")
                self.event("run_stopped", phase=self.state.phase)
            except Exception as exc:
                message = _failure_message(exc)
                self.state.error = message
                self.transition(phase="ESCALATED")
                self.event("escalated", error=message)
                try:
                    await self.finish_report("ESCALATED")
                except (OSError, ValueError, RuntimeError) as report_error:
                    self.event("report_error", error=str(report_error))
                if isinstance(exc, BaseExceptionGroup):
                    raise Escalation(message) from exc
                raise
            finally:
                tasks = [task for task in (stage_task, monitor) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await self.runner.cancel_all()
