"""Atomic snapshots, a durable event journal, and human-readable run evidence."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import portalocker
from pydantic import ValidationError

from orch.contracts import (
    REVIEW_APPLICATION_OPERATION,
    SUPPORTED_STATE_SCHEMA_VERSIONS,
    ContractError,
    EvidenceConflictError,
    ReviewApplication,
    RunState,
)

# Internal writer locks tolerate brief contention (for example a control
# command racing a snapshot refresh) without ever weakening exclusivity.
WRITER_LOCK_TIMEOUT = 5.0


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _decision_digest(decision: dict | None) -> str | None:
    """Stable digest of one applied entry decision for audit reconciliation."""
    if decision is None:
        return None
    payload = json.dumps(decision, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def atomic_write(path: Path, text: str) -> None:
    """Durably replace one file; readers see the previous or the new content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(3):
            try:
                os.replace(name, path)
                break
            except PermissionError:
                # Windows readers can briefly hold the destination; retry
                # before reporting the replacement as failed.
                if attempt == 2:
                    raise
                time.sleep(0.05)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def _safe_component(value: str, label: str) -> str:
    if not value or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                        for c in value):
        raise ValueError(f"Unsafe {label}: {value!r}")
    return value


def _next_action(state: RunState) -> str:
    # Mirrors workflow.next_action so checklist output stays consistent even
    # when rendered before the workflow module is imported.
    if state.phase in {"DRAFT", "DISCUSSING"}:
        return "PLAN"
    if state.phase == "PLAN_READY":
        return "APPROVE"
    if state.phase in {"APPROVED", "RUNNING"}:
        return state.stage
    return {"PAUSED": "RESUME", "ESCALATED": "RESUME", "AWAITING_TEST": "HUMAN_TEST",
            "DONE": "NONE", "ABORTED": "NONE"}.get(state.phase, "UNKNOWN")


def _one_line(value: object) -> str:
    return " ".join(str(value).split())


def issues_markdown(state: RunState, *, include_ownership: bool = True) -> str:
    """Render the issue register without duplicating state.

    ``include_ownership=False`` reproduces the pre-ownership format used to
    verify a legacy pre-batch register snapshot during audited recovery.
    """
    issues = [f"# Issue register ({state.slug})", ""]
    if not state.issues:
        issues.append("_No issues recorded._")
    for issue in state.issues.values():
        parent = issue.parent_issue_id or "none"
        ownership = (f"; role={issue.owner_role}"
                     if include_ownership and issue.owner_role else "")
        legacy = f"; legacy={issue.legacy_status}" if issue.legacy_status else ""
        issues.append(f"- **{issue.id}** — status={issue.status}; depth={issue.depth}; "
                      f"owner={issue.worker}; root={issue.root_issue_id}; parent={parent}; "
                      f"attempt={issue.attempt}; reassignments={issue.reassignments}"
                      f"{ownership}; lifecycle={issue.lifecycle}{legacy}")
        for number, assignment in enumerate(issue.assignments, start=1):
            issues.append(f"  - assignment {number}: repairs={assignment.repairs}; "
                          f"failed={assignment.failed_repairs}; "
                          f"review_rounds={assignment.review_rounds}")
            for outcome in assignment.outcomes:
                issues.append(
                    f"  - outcome {outcome.repair_ordinal}: {outcome.outcome}; "
                    f"charged={outcome.charged_failed_repair}; "
                    f"transaction={outcome.review_transaction}")
        if issue.summary:
            issues.append(f"  - summary: {_one_line(issue.summary)}")
        for link in issue.evidence:
            issues.append(f"  - evidence: {link}")
    return "\n".join(issues) + "\n"


class StateStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()

    def run_dir(self, run_id: str) -> Path:
        return self.data_dir / "runs" / _safe_component(run_id, "run ID")

    @contextmanager
    def lock(self, name: str = "active", timeout: float = 0):
        _safe_component(name, "lock name")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self.data_dir / f"{name}.lock"), timeout=timeout):
            yield

    def load(self, run_id: str) -> RunState:
        path = self.run_dir(run_id) / "state.json"
        raw = path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractError(f"{path}: corrupt run state: {exc}") from exc
        if not isinstance(data, dict):
            raise ContractError(f"{path}: corrupt run state: expected a JSON object")
        if "version" not in data:
            raise ContractError(f"{path}: run state has no schema version; explicit migration is required")
        if data.get("version") not in SUPPORTED_STATE_SCHEMA_VERSIONS:
            raise ContractError(
                f"{path}: unsupported run state version {data.get('version')!r}; "
                "explicit migration is required"
            )
        try:
            return RunState.model_validate(data)
        except ValidationError as exc:
            raise ContractError(f"{path}: invalid run state: {exc}") from exc

    def save(self, state: RunState, expected_revision: int | None = None) -> RunState:
        with self.lock(f"state-{state.run_id}", timeout=WRITER_LOCK_TIMEOUT):
            path = self.run_dir(state.run_id) / "state.json"
            current = self.load(state.run_id) if path.exists() else None
            expected = state.revision if expected_revision is None else expected_revision
            if current and current.revision != expected:
                raise ContractError(
                    f"{path}: Stale state revision {expected}; disk has {current.revision}"
                )
            state = state.model_copy(deep=True)
            state.revision = (current.revision if current else 0) + 1
            state.updated_at = timestamp()
            atomic_write(path, state.model_dump_json(indent=2))
            self.render(state)
            return state

    def event(self, run_id: str, kind: str, **details) -> dict:
        """Append one durable event with a stable ID, then refresh the log.

        The journal is replaced atomically rather than appended so a crash can
        never leave a torn tail that hides a dispatch intent.
        """
        with self.lock(f"events-{run_id}", timeout=WRITER_LOCK_TIMEOUT):
            path = self.run_dir(run_id) / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            events = self.events(run_id)
            event = {"id": len(events) + 1, "time": timestamp(), "kind": kind, **details}
            atomic_write(path, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in [*events, event]))
            self._render_log_locked(run_id)
            return event

    def events(self, run_id: str) -> list[dict]:
        path = self.run_dir(run_id) / "events.jsonl"
        if not path.exists():
            return []
        events: list[dict] = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ContractError(f"{path}:{number}: corrupt event journal: {exc}") from exc
            if not isinstance(event, dict) or not isinstance(event.get("id"), int):
                raise ContractError(f"{path}:{number}: corrupt event journal entry")
            events.append(event)
        return events

    def record_operation(self, state: RunState, key: str, value: dict) -> RunState:
        """Persist a stable operation ID before side effects and merge its result after.

        The first call records intent; a later call with the same key merges
        completion details, so reconciliation can distinguish an interrupted
        operation from one that never started.
        """
        reserved = {"id", "time", "kind", "operation"}
        payload = {k: v for k, v in value.items() if k not in reserved}
        merged = dict(state.operations.get(key, {}))
        first_seen = merged.get("first_seen")
        merged.update(payload)
        merged.setdefault("first_seen", timestamp())
        if first_seen:
            merged["first_seen"] = first_seen
        merged["updated_at"] = timestamp()
        state.operations[key] = merged
        state = self.save(state)
        self.event(state.run_id, "operation", operation=key, **payload)
        return state

    def _application(self, state: RunState, application_id: str) -> ReviewApplication:
        application = state.review_applications.get(application_id)
        if application is None:
            raise ContractError(
                f"Review application {application_id} has no durable intent; "
                "nothing may be applied before the intent is persisted"
            )
        return application

    @staticmethod
    def _entry(application: ReviewApplication, entry_id: str):
        for entry in application.entries:
            if entry.entry_id == entry_id:
                return entry
        raise ContractError(
            f"Review application {application.application_id} has no entry {entry_id!r}"
        )

    def begin_review_application(self, state: RunState,
                                 application: ReviewApplication) -> RunState:
        """Persist the complete review intent before any entry is applied.

        Repeating an identical intent is idempotent. A different intent for the
        same application identity is contradictory and refused.
        """
        existing = state.review_applications.get(application.application_id)
        if existing is not None:
            def core(value: ReviewApplication) -> dict:
                payload = value.model_dump(mode="json")
                payload.pop("intent_at", None)
                return payload
            if core(existing) != core(application):
                raise EvidenceConflictError(
                    f"Review application {application.application_id} already has a "
                    "different durable intent"
                )
            return state
        if not application.entries:
            raise ContractError("A review application with no entries cannot be recorded")
        state.review_applications[application.application_id] = application.model_copy(deep=True)
        operation_key = f"{REVIEW_APPLICATION_OPERATION}{application.application_id}"
        return self.record_operation(state, operation_key, {
            "status": "intent",
            "transaction": application.transaction,
            "reviewer_role": application.reviewer_role,
            "candidate_sha": application.candidate_sha,
            "verdict_sha256": application.verdict_sha256,
            "entry_ids": [entry.entry_id for entry in application.entries],
            "intent_at": application.intent_at,
        })

    def apply_review_entry(self, state: RunState, application_id: str, entry_id: str, *,
                           decision: dict, issue) -> RunState:
        """Atomically save one entry's issue effects and applied decision.

        No audit event is written here: a crash between this save and
        ``record_review_entry_audit`` is a supported boundary that reconciliation
        repairs from the durable decision.
        """
        application = self._application(state, application_id)
        entry = self._entry(application, entry_id)
        if not isinstance(decision, dict) or not decision:
            raise ContractError(f"Entry {entry_id} requires an explicit nonempty decision")
        if decision.get("issue", entry.issue_id) != entry.issue_id:
            raise EvidenceConflictError(
                f"Entry {entry_id} decision names {decision.get('issue')!r}, not "
                f"{entry.issue_id!r}"
            )
        if entry.state == "applied":
            if entry.decision != decision:
                raise EvidenceConflictError(
                    f"Entry {entry_id} was already applied with a different decision"
                )
            return state
        if issue is None or issue.id != entry.issue_id:
            raise ContractError(
                f"Entry {entry_id} must supply the updated issue record for {entry.issue_id}"
            )
        current = state.issues.get(entry.issue_id)
        if (entry.issue_before and current is not None
                and current.model_dump(mode="json") != entry.issue_before):
            raise EvidenceConflictError(
                f"Entry {entry_id} has stale pre-application evidence; the issue "
                "changed after the review intent was persisted"
            )
        state.issues[entry.issue_id] = issue.model_copy(deep=True)
        updated = entry.model_copy(update={
            "state": "applied", "decision": dict(decision), "applied_at": timestamp()})
        application.entries[entry.order_index] = updated
        return self.save(state)

    def record_review_entry_audit(self, state: RunState, application_id: str,
                                  entry_id: str) -> RunState:
        """Append the stable audit event for one already-applied entry."""
        application = self._application(state, application_id)
        entry = self._entry(application, entry_id)
        if entry.state != "applied":
            raise ContractError(
                f"Entry {entry_id} cannot be audited before its decision is applied"
            )
        digest = _decision_digest(entry.decision)
        matching = [event for event in self.events(state.run_id)
                    if event.get("kind") == "review_entry_applied"
                    and event.get("application_id") == application_id
                    and event.get("entry_id") == entry_id]
        if len(matching) > 1:
            raise EvidenceConflictError(
                f"Entry {entry_id} has multiple audit events"
            )
        if matching:
            if matching[0].get("decision_digest") != digest:
                raise EvidenceConflictError(
                    f"Entry {entry_id} audit event disagrees with its durable decision"
                )
            return state
        self.event(
            state.run_id, "review_entry_applied",
            application_id=application_id, entry_id=entry_id,
            transaction=application.transaction,
            reviewer_role=application.reviewer_role,
            decision=entry.decision, decision_digest=digest,
        )
        return state

    def reconcile_review_application(self, state: RunState,
                                     application_id: str) -> RunState:
        """Repair missing audit events from durable decisions, or refuse.

        An audit event without an applied decision is a contradictory state and
        is refused rather than retroactively "applied" from the journal.
        """
        application = self._application(state, application_id)
        events = self.events(state.run_id)
        audited = {event.get("entry_id") for event in events
                   if event.get("kind") == "review_entry_applied"
                   and event.get("application_id") == application_id}
        for entry in application.entries:
            if entry.state == "not_applied" and entry.entry_id in audited:
                raise EvidenceConflictError(
                    f"Entry {entry.entry_id} has audit evidence but no applied decision"
                )
        for entry in application.entries:
            if entry.state == "applied":
                state = self.record_review_entry_audit(state, application_id, entry.entry_id)
        return state

    def complete_review_application(self, state: RunState,
                                    application_id: str) -> RunState:
        """Mark completion only when every entry and audit event agree."""
        application = self._application(state, application_id)
        if not application.fully_applied:
            missing = [entry.entry_id for entry in application.entries
                       if entry.state != "applied"]
            raise ContractError(
                f"Review application {application_id} is not complete; "
                f"unapplied entries: {', '.join(missing)}"
            )
        state = self.reconcile_review_application(state, application_id)
        application = self._application(state, application_id)
        if not application.complete:
            application.complete = True
            application.completed_at = timestamp()
            state = self.save(state)
        completed = [event for event in self.events(state.run_id)
                     if event.get("kind") == "review_application_completed"
                     and event.get("application_id") == application_id]
        if len(completed) > 1:
            raise EvidenceConflictError(
                f"Review application {application_id} has multiple completion events"
            )
        if not completed:
            self.event(
                state.run_id, "review_application_completed",
                application_id=application_id, transaction=application.transaction,
                applied_entry_ids=application.applied_entry_ids,
                completed_at=application.completed_at,
            )
        return state

    def resume_review_application(self, state: RunState, application_id: str) -> dict:
        """Resume an interrupted application before relevant dispatch.

        Reconciles missing audit events, finishes completion when possible, and
        reports the derived next-entry position. It never launches a worker.
        """
        application = state.review_applications.get(application_id)
        if application is None:
            return {"status": "absent", "application_id": application_id,
                    "next_entry_index": None, "complete": False}
        state = self.reconcile_review_application(state, application_id)
        application = self._application(state, application_id)
        if application.fully_applied:
            state = self.complete_review_application(state, application_id)
        application = self._application(state, application_id)
        return {
            "status": "completed" if application.complete else "pending",
            "application_id": application_id,
            "next_entry_index": application.next_entry_index,
            "complete": application.complete,
            "applied_entry_ids": application.applied_entry_ids,
        }

    def render_log(self, run_id: str) -> None:
        with self.lock(f"events-{run_id}", timeout=WRITER_LOCK_TIMEOUT):
            self._render_log_locked(run_id)

    def _render_log_locked(self, run_id: str) -> None:
        lines = [f"# Activity log ({run_id})", "",
                 "| ID | UTC time | Event | Details |", "|---|---|---|---|"]
        for event in self.events(run_id):
            detail = json.dumps({k: v for k, v in event.items() if k not in {"id", "time", "kind"}},
                                ensure_ascii=False)
            detail = detail.replace("|", "&#124;").replace("\n", " ")
            lines.append(f"| {event['id']} | {event['time']} | {_one_line(event['kind'])} | {detail} |")
        atomic_write(self.run_dir(run_id) / "activity_log.md", "\n".join(lines) + "\n")

    def render(self, state: RunState, *, next_action: str | None = None) -> None:
        """Refresh checklist.md, issues.md and activity_log.md for one run.

        These are atomically replaced snapshots maintained throughout
        execution; after a crash they are regenerated from the authoritative
        state and event journal without duplicating event IDs.
        """
        action = next_action or _next_action(state)
        lines = [
            f"# {state.slug}: checklist", "",
            f"- Phase: {state.phase}; stage: {state.stage}; round: {state.round}",
            f"- Next action: {action}",
            f"- Updated: {state.updated_at or timestamp()}",
            "",
            "## Agents",
            "",
        ]
        for agent, info in state.workers.items():
            status = str(info.get("status", "pending"))
            fields = [f"status={status}"]
            for key in ("role", "attempt", "attempt_id", "branch", "worktree", "checks",
                        "handover", "feedback", "sha", "activity"):
                if info.get(key) is not None:
                    fields.append(f"{key}={_one_line(info[key])}")
            if info.get("infrastructure_retries"):
                fields.append(f"retries={info['infrastructure_retries']}")
            if info.get("error"):
                fields.append(f"blocker={_one_line(info['error'])}")
            done = "x" if status in {"done", "resolved"} else " "
            lines.append(f"- [{done}] {agent}: " + "; ".join(fields))
        if state.review_results:
            lines.extend(["", "## Review results", ""])
            for key, result in sorted(state.review_results.items()):
                lines.append(
                    f"- {result.role}: {result.result}; candidate={result.candidate_sha}; "
                    f"attempt={result.attempt_id}; evidence={result.evidence_digest[:12]}; "
                    f"transaction={result.transaction}"
                )
        if state.error:
            lines.extend(["", f"Blocker: {_one_line(state.error)}"])
        atomic_write(self.run_dir(state.run_id) / "checklist.md", "\n".join(lines) + "\n")

        atomic_write(self.run_dir(state.run_id) / "issues.md", issues_markdown(state))
        self.render_log(state.run_id)
