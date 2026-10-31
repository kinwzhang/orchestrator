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

from orch.config import Config
from orch.contracts import (
    ApprovalRequiredError,
    AssignmentState,
    CheckError,
    ContractError,
    Discussion,
    FeatureSpec,
    Finding,
    Handover,
    IssueState,
    Manifest,
    ReviewResult,
    RunState,
    Verdict,
    approval_hashes,
    approved_files,
    read_markdown,
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
_REVIEW_BATCH_RECOVERY_PREFIX = "review-batch-recovery:"
_LEGACY_REVIEW_CONFLICT = "Issue is simultaneously resolved and open"
_SYSTEM_ISSUE_TEMPLATES = ("check-{}", "post-check-{}", "integration-check-{}")


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
class RoleRoutingPlan:
    """A pure, fully validated routing result for one reviewer role.

    Nothing in the caller's issue register is mutated while this plan is built.
    ``escalation`` records a reviewer-requested or exhausted budget boundary
    that must be committed with the accepted result before being re-raised.
    """

    issues: dict[str, IssueState]
    findings: dict[str, Finding]
    resolved: list[str]
    touched: list[str]
    affected_workers: list[str]
    escalation: str | None = None


def _validate_repair_counters(identity: str, assignment: AssignmentState) -> None:
    """Refuse internally contradictory repair counters before any routing."""
    if assignment.failed_repairs > assignment.repairs:
        raise ContractError(
            f"Issue {identity} records {assignment.failed_repairs} failed repairs for "
            f"{assignment.repairs} repair attempt(s). Counters are inconsistent, so the "
            "assessment history cannot be trusted. Refusing to reset or reinterpret them; "
            "audit the retained review transactions and use an approved recovery."
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


def repair_assessment_evidence(
    state: RunState,
) -> dict[tuple[str, int], dict[int, dict[str, list[str]]]]:
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
    evidence: dict[tuple[str, int], dict[int, dict[str, list[str]]]] = {}
    completed: set[str] = set()
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
            record = evidence.setdefault((identity, assignment_index), {}).setdefault(
                ordinal, {"findings": [], "resolved": []}
            )
            record["findings" if identity in finding_ids else "resolved"].append(key)
    missing = sorted(set(state.review_results) - completed)
    if missing:
        raise ContractError(
            "Accepted role results have no completed review transaction: "
            + ", ".join(missing)
            + "; repair-assessment provenance cannot be established"
        )
    return evidence


def _derive_assessed_repairs(identity: str, assignment: AssignmentState,
                             records: dict[int, dict[str, list[str]]]) -> int:
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
    failure_ordinals = {ordinal for ordinal, record in records.items() if record["findings"]}
    if assignment.failed_repairs > len(failure_ordinals):
        raise ContractError(
            f"Issue {identity} records {assignment.failed_repairs} failed repair(s) but "
            f"completed reviews show only {len(failure_ordinals)} distinct unsuccessful "
            "repair outcome(s). The counters may contain a duplicate charge from an "
            "earlier revalidation. Refusing to guess or reset them; audit the retained "
            "review transactions and use an approved recovery."
        )
    if assignment.failed_repairs < len(failure_ordinals):
        raise ContractError(
            f"Issue {identity} records {assignment.failed_repairs} failed repair(s) but "
            f"completed reviews show {len(failure_ordinals)} distinct unsuccessful repair "
            "outcome(s). The counters understate the assessed history; refusing to continue."
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
    evidence: dict[tuple[str, int], dict[int, dict[str, list[str]]]],
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
        assignment = issue.assignments[-1]
        records = evidence.get((identity, len(issue.assignments) - 1), {})
        if identity in finding_ids and assignment.assessed_repairs is None:
            assignment.assessed_repairs = _derive_assessed_repairs(identity, assignment, records)
            continue
        _validate_repair_counters(identity, assignment)
        if assignment.assessed_repairs is None:
            continue
        if not records:
            # An explicit durable mark is itself provenance. Only the counter
            # invariants can be checked here; there is no completed transaction
            # to cross-validate against.
            continue
        evidence_mark = max(records)
        if assignment.assessed_repairs != evidence_mark:
            raise ContractError(
                f"Issue {identity} records repair outcome {assignment.assessed_repairs} "
                f"as assessed but retained reviews assessed outcome {evidence_mark}. "
                "Explicit provenance disagrees with the retained evidence; refusing to continue."
            )
        failure_ordinals = {ordinal for ordinal, record in records.items()
                            if record["findings"]}
        if assignment.failed_repairs != len(failure_ordinals):
            raise ContractError(
                f"Issue {identity} records {assignment.failed_repairs} failed repair(s) but "
                f"retained reviews show {len(failure_ordinals)} distinct unsuccessful "
                "outcome(s). Explicit provenance disagrees with the retained evidence; "
                "refusing to continue."
            )
    return work


def _assess_repair_outcome(issue: IssueState, identity: str) -> bool:
    """Return whether a finding assesses a distinct unsuccessful repair.

    ``repairs`` counts worker dispatch attempts for the issue. ``assessed_repairs``
    records the highest repair ordinal whose outcome a reviewer has already
    judged, so each actual repair is charged at most once even when a changed
    approval or a fresh reviewer run routes the same retained feedback again.

    A missing mark is only safe for an open issue with no failure or review
    history; all other legacy histories must be normalised from completed review
    evidence by the caller first. Counters alone never prove an assessment.

    The caller owns a deep copy, so mutating the counters here cannot affect the
    accepted register unless the whole plan validates.
    """
    current = issue.assignments[-1]
    _validate_repair_counters(identity, current)
    if current.repairs == 0:
        if current.assessed_repairs is None:
            current.assessed_repairs = 0
        return False
    if current.assessed_repairs is None:
        if current.failed_repairs or current.review_rounds or issue.status == "resolved":
            raise ContractError(
                f"Issue {identity} has repair history without durable assessment "
                "provenance. Counters alone cannot prove which repair outcome was "
                "assessed. Refusing to guess; derive the mark from completed review "
                "transactions or use an approved recovery."
            )
        current.assessed_repairs = current.repairs
        return True
    if current.repairs > current.assessed_repairs:
        current.assessed_repairs = current.repairs
        return True
    return False


def plan_role_routing(issues: dict[str, IssueState], verdict: Verdict,
                      manifest: Manifest, role: str) -> RoleRoutingPlan:
    """Validate one role's verdict and compute its issue changes without mutating state.

    Ownership, lineage, within-role pass completeness and budgets are checked
    here. A rejected verdict therefore never has to be rolled back because no
    counter or status was changed before the whole plan validated.
    """
    if role not in _REVIEW_ROLES:
        raise ContractError(f"Unknown reviewer role: {role!r}")
    if verdict.reviewer != role:
        raise ContractError(f"Verdict identity {verdict.reviewer!r} does not match role {role!r}")
    workers = {w.id for w in manifest.workers}
    work = {identity: issue.model_copy(deep=True) for identity, issue in issues.items()}
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
                         if issue.owner_role == role and issue.status == "open"
                         and issue.id not in resolved)
        if missing:
            raise ContractError("Passing verdict must explicitly resolve " + ", ".join(missing))

    touched: set[str] = set()
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
            if previous:
                if (previous.parent_issue_id, previous.worker) != (finding.parent_issue_id, finding.worker):
                    raise ContractError(f"Issue identity changed: {identity}")
                current = previous.assignments[-1]
                new_failed_repair = _assess_repair_outcome(previous, identity)
                if new_failed_repair:
                    current.failed_repairs += 1
                    current.review_rounds += 1
                previous.status = "open"
            else:
                previous = IssueState(id=identity, worker=finding.worker, root_issue_id=root,
                                      parent_issue_id=finding.parent_issue_id, depth=depth,
                                      summary=finding.summary, owner_role=role)
                work[identity] = previous
            touched.add(identity)
            if depth > manifest.limits.max_issue_depth:
                work[root].status = "escalated"
                touched.add(root)
                return RoleRoutingPlan(
                    issues=work, findings=findings, resolved=[], touched=sorted(touched),
                    affected_workers=sorted({f.worker for f in findings.values()}),
                    escalation=(f"Derived issue {identity} exceeds depth limit; "
                                f"escalate root {root}"),
                )
            if finding.action == "escalate":
                previous.status = "escalated"
                return RoleRoutingPlan(
                    issues=work, findings=findings, resolved=[], touched=sorted(touched),
                    affected_workers=sorted({f.worker for f in findings.values()}),
                    escalation=f"Issue {identity} requires escalation",
                )
            current = previous.assignments[-1]
            exhausted = (current.failed_repairs >= manifest.limits.max_failed_repairs_per_assignment
                         or current.repairs >= manifest.limits.max_repair_attempts
                         or current.review_rounds >= manifest.limits.max_review_rounds)
            if exhausted or finding.action == "reassign":
                if len(previous.assignments) - 1 >= manifest.limits.max_reassignments:
                    previous.status = "escalated"
                    return RoleRoutingPlan(
                        issues=work, findings=findings, resolved=[], touched=sorted(touched),
                        affected_workers=sorted({f.worker for f in findings.values()}),
                        escalation=f"Issue {identity} exhausted replacement allowance",
                    )
                previous.assignments.append(AssignmentState())
            del remaining[identity]
            progress = True
        if not progress:
            raise ContractError("Issue lineage has missing parents or a cycle")

    for identity in resolved:
        issue = work[identity]
        was_open = issue.status == "open"
        _validate_repair_counters(identity, issue.assignments[-1])
        issue.status = "resolved"
        # A passing assessment covers the current repair outcome even though it
        # is not a failure. Record the mark so a later revalidation of the same
        # outcome cannot charge it as failed. A newly resolved outcome counts as
        # a review round; re-resolving an already resolved outcome does not.
        current = issue.assignments[-1]
        new_outcome = (current.assessed_repairs is None
                       or current.repairs > current.assessed_repairs)
        if was_open and new_outcome:
            current.review_rounds += 1
        if current.assessed_repairs is None:
            current.assessed_repairs = current.repairs
        else:
            current.assessed_repairs = max(current.assessed_repairs, current.repairs)
        touched.add(identity)
    return RoleRoutingPlan(
        issues=work, findings=findings, resolved=resolved, touched=sorted(touched),
        affected_workers=sorted({f.worker for f in findings.values()}),
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
    plan = plan_role_routing(base_issues, verdict, manifest, role)
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
    merged = dict(state.issues)
    for identity in plan.touched:
        merged[identity] = plan.issues[identity].model_copy(deep=True)
    state.issues = merged
    state.review_results[key] = result
    state = store.save(state)

    events = store.events(state.run_id)
    matching = _review_acceptance_events(events, key)
    if len(matching) > 1:
        raise ContractError(f"Review transaction {key} has multiple acceptance events")
    if matching:
        event = matching[0]
    else:
        event = store.event(
            state.run_id, "review_result_accepted", transaction=key, role=role,
            candidate_sha=candidate_sha, evidence_digest=evidence_digest,
            attempt_id=result.attempt_id, verdict_sha256=result.verdict_sha256,
            verdict_path=result.verdict_path, result=result.result,
            findings=result.findings, resolved=result.resolved,
            affected_workers=result.affected_workers, escalation=result.escalation,
            binding=result.binding,
        )
    state = store.record_operation(state, key, {
        "status": "completed", "event_id": event["id"],
        "after_issues_digest": _issues_digest(state.issues),
        "completed_at": timestamp(),
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
    plan_role_routing(base_issues, verdict, manifest, role)
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
    }
    state = store.record_operation(state, key, operation)
    return _finish_review_operation(store, state, key, state.operations[key], manifest)


def review_transaction_gaps(state: RunState, events: list[dict]) -> list[str]:
    """Report incomplete or unaudited role transactions that block review."""
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
    for key in state.review_results:
        operation = state.operations.get(key)
        if not isinstance(operation, dict) or operation.get("status") != "completed":
            gaps.append(f"{key} (role result has no completed transaction)")
    return sorted(set(gaps))


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
        if issue.status != "open":
            raise ContractError(
                f"Issue {identity} is {issue.status}, not open; this is not the expected "
                "partially applied review batch"
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
        if issue.status != "open":
            raise ContractError(
                f"Issue {identity} is already {issue.status}; resolutions from this batch "
                "were applied, so the generic batch recovery does not apply"
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


class Engine:
    def __init__(self, store: StateStore, state: RunState, feature: FeatureSpec,
                 manifest: Manifest, plan: Path, *, adapters=None, runner=None, config: Config | None = None):
        from orch.agents import CodexAdapter, OpenCodeAdapter
        from orch.git_ops import GitWorkspace, WorkspaceRecord
        from orch.processes import ProcessRunner
        self.store, self.state, self.feature = store, state, feature
        self.config = config or Config()
        self.manifest, self.plan = manifest, plan.resolve()
        self.run_dir = store.run_dir(state.run_id)
        self.git = GitWorkspace(feature.target_project, self.run_dir)
        self.record = WorkspaceRecord.from_dict(state.workspace)
        self.runner = runner or ProcessRunner()
        self.adapters = adapters or {"planner": CodexAdapter(self.runner), "opencode": OpenCodeAdapter(self.runner)}

    def save(self):
        saved = self.store.save(self.state)
        self.state.revision = saved.revision

    def event(self, kind, **details):
        self.store.event(self.state.run_id, kind, **details)

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
        self.save()
        self.event("transition", phase=self.state.phase, stage=self.state.stage, cycle=self.state.round)

    def check_approval(self):
        actual = approval_hashes(self.plan, self.manifest, self.feature)
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
            if kind == "process_started":
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

    async def run_worker(self, worker):
        self.check_recovery_audit()
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
        issues = [i for i in self.state.issues.values() if i.worker == worker.id and i.status == "open"]
        while True:
            self.check_control()
            self.check_approval()
            issues = [i for i in self.state.issues.values() if i.worker == worker.id and i.status == "open"]
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
                issue.assignments[-1].repairs += 1
                issue.attempt = attempt
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
                check_issue = self.state.issues.get(f"check-{worker.id}")
                if check_issue:
                    check_issue.status = "resolved"
                mutation_issue = self.state.issues.get(f"post-check-{worker.id}")
                if mutation_issue:
                    mutation_issue.status = "resolved"
                info.update(sha=sha, checked=True, status="done", checks="pass")
                self.state.workspace = {**self.state.workspace, **self.record.to_dict()}
                self.save()
                return
            except (ContractError, CheckError, OSError) as exc:
                self.event("worker_check_failed", agent=worker.id, error=str(exc))
                key = f"check-{worker.id}"
                if key not in self.state.issues:
                    self.state.issues[key] = IssueState(id=key, worker=worker.id, root_issue_id=key,
                                                      summary=str(exc), owner_role="system")
                issue = self.state.issues[key]
                current = issue.assignments[-1]
                if current.repairs:
                    current.failed_repairs += 1
                if current.failed_repairs >= self.manifest.limits.max_failed_repairs_per_assignment:
                    if len(issue.assignments) - 1 >= self.manifest.limits.max_reassignments:
                        raise Escalation(f"Check repair exhausted for {worker.id}") from exc
                    issue.assignments.append(AssignmentState())
                feedback = self.run_dir / f"round-{self.state.round}" / "feedback" / f"{worker.id}-{attempt}.md"
                atomic_write(feedback, f"# CHECK feedback\n\n{exc}\n")
                info["feedback"] = str(feedback)
                issues = [i for i in self.state.issues.values() if i.worker == worker.id and i.status == "open"]
                self.save()

    async def work(self):
        pending = {w.id: w for w in self.manifest.workers if not self.state.workers.get(w.id, {}).get("checked")}
        while pending:
            ready = [w for w in pending.values() if all(self.state.workers.get(d, {}).get("checked") for d in w.depends_on)]
            if not ready:
                raise Escalation("No schedulable workers; dependency state is inconsistent")
            semaphore = asyncio.Semaphore(self.manifest.limits.concurrency)
            async def one(worker, semaphore=semaphore):
                async with semaphore:
                    await self.run_worker(worker)
            async with asyncio.TaskGroup() as group:
                for worker in ready:
                    group.create_task(one(worker))
            for worker in ready:
                del pending[worker.id]

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
                self.state.issues[issue_id] = IssueState(
                    id=issue_id, worker=worker_id, root_issue_id=issue_id,
                    summary="Worker worktree changed after its successful CHECK; revalidate retained changes.",
                    owner_role="system",
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
            updated, result = _finish_review_operation(
                self.store, self.state, key, operation, self.manifest
            )
            self._adopt_review_state(updated)
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
                updated, result = accept_review_result(
                    self.store, self.state, role=role, candidate_sha=candidate.sha,
                    evidence_digest=evidence_digest,
                    attempt_id=str(self.state.workers[logical].get("attempt_id")),
                    verdict=verdict, verdict_path=output, manifest=self.manifest,
                )
                self._adopt_review_state(updated)
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
                        issue = IssueState(id=key, worker=worker.id, root_issue_id=key,
                                           summary=str(exc), owner_role="system")
                        self.state.issues[key] = issue
                    else:
                        current = issue.assignments[-1]
                        current.failed_repairs += 1
                        if current.failed_repairs >= self.manifest.limits.max_failed_repairs_per_assignment:
                            if len(issue.assignments) - 1 >= self.manifest.limits.max_reassignments:
                                raise Escalation("Integration check repairs exhausted") from exc
                            issue.assignments.append(AssignmentState())
                    info = self.state.workers[worker.id]
                    info.update(checked=False, status="rework")
                    feedback = self.run_dir / f"round-{self.state.round}" / "feedback" / f"integration-{worker.id}.md"
                    atomic_write(feedback, f"# Integration CHECK failure\n\n{exc}\n\nInspect your owned changes and handovers; do not edit another worker's scope.\n")
                    info["feedback"] = str(feedback)
                self.event("integration_check_rework", error=str(exc))
                self.state.round += 1
                self.transition(stage="WORK")
                return
            for issue in self.state.issues.values():
                if issue.id.startswith("integration-check-"):
                    issue.status = "resolved"
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
            if failures or escalations:
                details = failures + escalations
                raise Escalation("Independent review role failure: " + "; ".join(details))
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
                system_open = sorted(issue.id for issue in self.state.issues.values()
                                     if issue.owner_role == "system" and issue.status == "open")
                if system_open:
                    raise Escalation(
                        "Open workflow-owned issue blocks promotion: " + ", ".join(system_open)
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
