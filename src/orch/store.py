"""Atomic snapshots, a durable event journal, and human-readable run evidence."""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import portalocker
from pydantic import ValidationError

from orch.contracts import ContractError, RunState

# Internal writer locks tolerate brief contention (for example a control
# command racing a snapshot refresh) without ever weakening exclusivity.
WRITER_LOCK_TIMEOUT = 5.0


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


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
        issues.append(f"- **{issue.id}** — status={issue.status}; depth={issue.depth}; "
                      f"owner={issue.worker}; root={issue.root_issue_id}; parent={parent}; "
                      f"attempt={issue.attempt}; reassignments={issue.reassignments}"
                      f"{ownership}")
        for number, assignment in enumerate(issue.assignments, start=1):
            issues.append(f"  - assignment {number}: repairs={assignment.repairs}; "
                          f"failed={assignment.failed_repairs}; "
                          f"review_rounds={assignment.review_rounds}")
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
        if data.get("version") != 1:
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
