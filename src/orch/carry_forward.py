"""One-shot carry-forward import of version-one run history.

The importer is deliberately separate from the workflow: it reads one reviewed
inventory Markdown file and copies its issues, counters, dispositions and
evidence references into an existing canonical run.  It never creates a run,
never dispatches work and never writes to the predecessor run.

Crash-safe sequencing (all writes go through committed store authorities):

1. validate the complete inventory and refuse before any write;
2. record the intent operation (``carry_forward_import``) with its digest;
3. import one issue at a time with ``StateStore.save``;
4. append one ``carry_forward_issue_imported`` audit event per issue;
5. verify durable counters/evidence against the inventory digest;
6. record operation completion and the ``carry_forward_import_completed`` event.

A rerun is idempotent: a completed import reports without touching history, an
interrupted import with the same digest continues from the first unapplied
issue, and a different digest is refused.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, field_validator

from orch.contracts import (
    SYSTEM_ISSUE_SCOPE,
    AssignmentState,
    ContractError,
    IssueState,
    LifecycleStatus,
    RunState,
    StrictModel,
    matches,
    migrate_v1_state,
    read_markdown,
    require_operational_schema,
    safe_relative,
    validation_detail,
)
from orch.store import StateStore, timestamp

CARRY_FORWARD_IMPORT_OPERATION = "carry_forward_import"
CARRY_FORWARD_ISSUE_EVENT = "carry_forward_issue_imported"
CARRY_FORWARD_COMPLETED_EVENT = "carry_forward_import_completed"
INVENTORY_VERSION = 1

_LEGACY_STATUSES = ("open", "resolved", "escalated")
_LIFECYCLE_BY_LEGACY = {
    "open": ("pending", "in_repair"),
    "resolved": ("finished",),
    "escalated": ("escalated",),
}
_DISPOSITION_KINDS = ("continuation", "scoped_acceptance", "deferral")

_ISSUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ASSIGNMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

_REQUIRED_FIELDS = (
    "owner", "owner_role", "root", "parent", "depth", "status", "lifecycle",
    "reassignments", "dispositions", "uncertainties", "evidence",
)
_OPTIONAL_FIELDS = ("attempt", "summary")
_ALLOWED_FIELDS = frozenset((*_REQUIRED_FIELDS, *_OPTIONAL_FIELDS, "assignments"))
_FIELD_LINE = re.compile(r"- ([a-z_]+):(?: (.*))?")
_HEADING_LINE = re.compile(r"## Issue ([A-Za-z0-9][A-Za-z0-9._-]*)")
_ASSIGNMENT_LINE = re.compile(
    r"  - ([A-Za-z0-9][A-Za-z0-9._:-]*); repairs=([0-9]+); failed=([0-9]+); "
    r"rounds=([0-9]+)"
)


class CarryForwardError(ContractError):
    """Base class for carry-forward importer refusals."""


class CarryForwardRefused(CarryForwardError):
    """A reviewed inventory or target state that cannot be imported safely."""


class _InventoryFrontMatter(StrictModel):
    version: Literal[1]
    predecessor_run_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    predecessor_candidate_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_shas: dict[str, str]

    @field_validator("source_shas")
    @classmethod
    def _validate_source_shas(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("source_shas must name at least one source")
        for name, sha in value.items():
            if not isinstance(name, str) or not name.strip() or _CONTROL.search(name):
                raise ValueError(
                    "source_shas keys must be nonempty names without control characters"
                )
            if not isinstance(sha, str) or not _SHA40.fullmatch(sha):
                raise ValueError(
                    "source_shas values must be 40-character lowercase hexadecimal SHAs"
                )
        return value


@dataclass(frozen=True)
class InventoryAssignment:
    assignment_id: str
    repairs: int
    failed: int
    rounds: int

    def canonical(self) -> dict:
        return {
            "assignment_id": self.assignment_id,
            "repairs": self.repairs,
            "failed": self.failed,
            "rounds": self.rounds,
        }


@dataclass(frozen=True)
class InventoryIssue:
    id: str
    worker: str
    owner_role: Literal["code", "docs", "system"]
    root: str
    parent: str | None
    depth: int
    status: Literal["open", "resolved", "escalated"]
    lifecycle: LifecycleStatus
    attempt: int
    summary: str
    assignments: tuple[InventoryAssignment, ...]
    reassignments: int
    dispositions: tuple[str, ...]
    evidence: tuple[str, ...]
    uncertainties: tuple[str, ...]

    def canonical(self) -> dict:
        return {
            "id": self.id,
            "worker": self.worker,
            "owner_role": self.owner_role,
            "root": self.root,
            "parent": self.parent,
            "depth": self.depth,
            "status": self.status,
            "lifecycle": self.lifecycle,
            "attempt": self.attempt,
            "summary": self.summary,
            "assignments": [assignment.canonical() for assignment in self.assignments],
            "reassignments": self.reassignments,
            "dispositions": list(self.dispositions),
            "evidence": list(self.evidence),
            "uncertainties": list(self.uncertainties),
        }

    @property
    def issue_digest(self) -> str:
        return _digest(self.canonical())

    @property
    def has_repair_activity(self) -> bool:
        return any(
            assignment.repairs or assignment.failed or assignment.rounds
            for assignment in self.assignments
        )

    def assignment_ids(self) -> list[str]:
        return [assignment.assignment_id for assignment in self.assignments]


@dataclass(frozen=True)
class CarryForwardInventory:
    version: int
    predecessor_run_id: str
    predecessor_candidate_sha: str
    inventory_digest: str
    source_shas: dict[str, str]
    issues: tuple[InventoryIssue, ...]

    def canonical(self) -> dict:
        return {
            "version": self.version,
            "predecessor_run_id": self.predecessor_run_id,
            "predecessor_candidate_sha": self.predecessor_candidate_sha,
            "source_shas": dict(self.source_shas),
            "issues": [issue.canonical() for issue in self.issues],
        }

    @property
    def computed_digest(self) -> str:
        return _digest(self.canonical())

    def verify_digest(self) -> None:
        computed = self.computed_digest
        if computed != self.inventory_digest:
            raise CarryForwardRefused(
                "inventory digest mismatch: declared "
                f"{self.inventory_digest}, computed {computed}"
            )

    @property
    def issue_ids(self) -> list[str]:
        return [issue.id for issue in self.issues]

    def issue(self, identity: str) -> InventoryIssue:
        for issue in self.issues:
            if issue.id == identity:
                return issue
        raise CarryForwardRefused(f"inventory has no issue {identity!r}")


@dataclass(frozen=True)
class CarryForwardReport:
    status: Literal["imported", "resumed", "completed"]
    run_id: str
    inventory_digest: str
    predecessor_run_id: str
    imported_issues: tuple[str, ...]
    already_imported_issues: tuple[str, ...]
    audit_event_ids: tuple[int, ...]
    completion_event_id: int | None


def _digest(value: object) -> str:
    """Canonical SHA-256 used for both the inventory and one issue record."""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _body_error(path: Path, index: int, message: str) -> CarryForwardRefused:
    return CarryForwardRefused(f"{path}:{index + 1}: {message}")


def _list_items(value: str, *, label: str, path: Path, index: int) -> tuple[str, ...]:
    if value == "none":
        return ()
    if not value:
        raise _body_error(path, index, f"{label} must be 'none' or a nonempty list")
    items = value.split("; ")
    for item in items:
        if not item or item != item.strip() or ";" in item or _CONTROL.search(item):
            raise _body_error(
                path, index, f"{label} items must be nonempty and separated by '; '"
            )
    return tuple(items)


def _parse_identity(value: str, pattern: re.Pattern[str], *, label: str,
                    path: Path, index: int) -> str:
    if not pattern.fullmatch(value):
        raise _body_error(path, index, f"{label} is not a valid identifier")
    return value


def _parse_int(value: str, *, label: str, minimum: int, path: Path, index: int) -> int:
    if not re.fullmatch(r"[0-9]+", value):
        raise _body_error(path, index, f"{label} must be a nonnegative integer")
    parsed = int(value)
    if parsed < minimum:
        raise _body_error(path, index, f"{label} must be at least {minimum}")
    return parsed


def _parse_dispositions(value: str, *, path: Path, index: int) -> tuple[str, ...]:
    items = _list_items(value, label="dispositions", path=path, index=index)
    for item in items:
        kind, separator, scope = item.partition(":")
        if not separator or kind not in _DISPOSITION_KINDS or not scope:
            raise _body_error(
                path, index,
                "dispositions must be '<kind>:<scope>' with kind continuation, "
                "scoped_acceptance or deferral",
            )
        if _CONTROL.search(scope):
            raise _body_error(path, index, "disposition scope contains control characters")
    return items


def _parse_evidence(value: str, *, path: Path, index: int) -> tuple[str, ...]:
    items = _list_items(value, label="evidence", path=path, index=index)
    for item in items:
        try:
            safe_relative(item)
        except ValueError as exc:
            raise _body_error(
                path, index, f"evidence reference is not a portable relative path: {item!r}"
            ) from exc
    return items


def _parse_uncertainties(value: str, *, path: Path, index: int) -> tuple[str, ...]:
    return _list_items(value, label="uncertainties", path=path, index=index)


def _parse_assignment(line: str, *, path: Path, index: int) -> InventoryAssignment:
    match = _ASSIGNMENT_LINE.fullmatch(line)
    if match is None:
        raise _body_error(
            path, index,
            "assignment must be '  - <id>; repairs=<n>; failed=<n>; rounds=<n>'",
        )
    assignment = InventoryAssignment(
        assignment_id=match.group(1),
        repairs=int(match.group(2)),
        failed=int(match.group(3)),
        rounds=int(match.group(4)),
    )
    if assignment.failed > assignment.repairs:
        raise _body_error(
            path, index,
            f"assignment {assignment.assignment_id} records more failed repairs than repairs",
        )
    return assignment


def _parse_field(line: str, *, path: Path, index: int) -> tuple[str, str]:
    match = _FIELD_LINE.fullmatch(line)
    if match is None:
        raise _body_error(path, index, "expected a '- key: value' field")
    value = match.group(2) or ""
    if value.startswith(" ") or value != value.rstrip():
        raise _body_error(
            path, index, "field values use exactly one space after ':' and no trailing space"
        )
    return match.group(1), value


def _build_issue(identity: str, fields: dict[str, str],
                 assignments: tuple[InventoryAssignment, ...] | None,
                 *, path: Path, index: int) -> InventoryIssue:
    missing = [name for name in _REQUIRED_FIELDS if name not in fields]
    if assignments is None:
        missing.append("assignments")
    if missing:
        raise _body_error(path, index, "missing required field(s): " + ", ".join(missing))
    assert assignments is not None
    owner = _parse_identity(fields["owner"], _WORKER_ID, label="owner", path=path, index=index)
    owner_role = fields["owner_role"]
    if owner_role not in ("code", "docs", "system"):
        raise _body_error(path, index, "owner_role must be code, docs or system")
    root = _parse_identity(fields["root"], _ISSUE_ID, label="root", path=path, index=index)
    parent_value = fields["parent"]
    parent = (None if parent_value == "none"
              else _parse_identity(parent_value, _ISSUE_ID, label="parent",
                                   path=path, index=index))
    depth = _parse_int(fields["depth"], label="depth", minimum=0, path=path, index=index)
    status = fields["status"]
    if status not in _LEGACY_STATUSES:
        raise _body_error(path, index, "status must be open, resolved or escalated")
    lifecycle = fields["lifecycle"]
    if lifecycle not in ("pending", "in_repair", "finished", "escalated"):
        raise _body_error(
            path, index, "lifecycle must be pending, in_repair, finished or escalated"
        )
    if lifecycle not in _LIFECYCLE_BY_LEGACY[status]:
        allowed = ", ".join(_LIFECYCLE_BY_LEGACY[status])
        raise _body_error(
            path, index,
            f"status {status!r} requires lifecycle {allowed}; import never reopens, "
            "releases or reclassifies a historical disposition",
        )
    attempt = _parse_int(fields.get("attempt", "1"), label="attempt", minimum=1,
                         path=path, index=index)
    summary = fields.get("summary", "")
    if _CONTROL.search(summary):
        raise _body_error(path, index, "summary contains control characters")
    reassignments = _parse_int(fields["reassignments"], label="reassignments", minimum=0,
                               path=path, index=index)
    if reassignments != len(assignments) - 1:
        raise _body_error(
            path, index,
            f"reassignments is {reassignments} but {len(assignments)} assignment(s) "
            "are listed",
        )
    dispositions = _parse_dispositions(fields["dispositions"], path=path, index=index)
    evidence = _parse_evidence(fields["evidence"], path=path, index=index)
    uncertainties = _parse_uncertainties(fields["uncertainties"], path=path, index=index)
    if owner_role == "system" and not any(
        matches(identity, pattern) for pattern in SYSTEM_ISSUE_SCOPE
    ):
        raise _body_error(
            path, index,
            f"system-owned issue {identity!r} is outside every workflow check scope",
        )
    return InventoryIssue(
        id=identity, worker=owner, owner_role=owner_role, root=root, parent=parent,
        depth=depth, status=status, lifecycle=lifecycle, attempt=attempt, summary=summary,
        assignments=assignments, reassignments=reassignments, dispositions=dispositions,
        evidence=evidence, uncertainties=uncertainties,
    )


def _validate_lineage(issues: tuple[InventoryIssue, ...], *, path: Path) -> None:
    by_id = {issue.id: issue for issue in issues}
    for issue in issues:
        if issue.parent is None:
            if issue.root != issue.id:
                raise CarryForwardRefused(
                    f"{path}: issue {issue.id}: a root issue must name itself as its root"
                )
            if issue.depth != 0:
                raise CarryForwardRefused(
                    f"{path}: issue {issue.id}: a root issue must have depth 0"
                )
            continue
        if issue.parent == issue.id:
            raise CarryForwardRefused(f"{path}: issue {issue.id} cannot be its own parent")
        parent = by_id.get(issue.parent)
        if parent is None:
            raise CarryForwardRefused(
                f"{path}: issue {issue.id}: unknown parent {issue.parent!r}"
            )
        if issue.root == issue.id:
            raise CarryForwardRefused(
                f"{path}: issue {issue.id}: a derived issue cannot be its own root"
            )
        if issue.root != parent.root:
            raise CarryForwardRefused(
                f"{path}: issue {issue.id}: root {issue.root!r} does not match its "
                f"parent's root {parent.root!r}"
            )
        if issue.depth != parent.depth + 1:
            raise CarryForwardRefused(
                f"{path}: issue {issue.id}: depth {issue.depth} does not follow parent "
                f"depth {parent.depth}"
            )
        seen = {issue.id}
        cursor: InventoryIssue | None = parent
        while cursor is not None:
            if cursor.id in seen:
                raise CarryForwardRefused(
                    f"{path}: issue {issue.id}: lineage contains a cycle"
                )
            seen.add(cursor.id)
            cursor = by_id.get(cursor.parent) if cursor.parent else None
    for issue in issues:
        root = by_id.get(issue.root)
        if root is None:
            raise CarryForwardRefused(f"{path}: issue {issue.id}: unknown root {issue.root!r}")
        if root.parent is not None or root.depth != 0:
            raise CarryForwardRefused(
                f"{path}: issue {issue.id}: root {issue.root!r} is not a root issue"
            )


def _parse_body(body: str, *, path: Path) -> tuple[InventoryIssue, ...]:
    lines = body.splitlines()
    issues: list[InventoryIssue] = []
    seen_issues: set[str] = set()
    seen_assignments: set[str] = set()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        heading = _HEADING_LINE.fullmatch(line)
        if heading is None:
            raise _body_error(path, index, "expected a '## Issue <id>' heading")
        identity = heading.group(1)
        if identity in seen_issues:
            raise _body_error(path, index, f"duplicate issue {identity!r}")
        seen_issues.add(identity)
        index += 1
        fields: dict[str, str] = {}
        assignments: tuple[InventoryAssignment, ...] | None = None
        while index < len(lines):
            line = lines[index]
            if not line.strip():
                index += 1
                continue
            if line.startswith("## "):
                break
            if line.startswith("  - "):
                raise _body_error(
                    path, index,
                    "assignment line must immediately follow '- assignments:'",
                )
            if not line.startswith("- "):
                raise _body_error(
                    path, index, "expected a '- key: value' field or a '## Issue' heading"
                )
            key, value = _parse_field(line, path=path, index=index)
            if key in fields:
                raise _body_error(path, index, f"duplicate field {key!r}")
            if key not in _ALLOWED_FIELDS:
                raise _body_error(path, index, f"unknown field {key!r}")
            if key == "assignments":
                if value:
                    raise _body_error(path, index, "'assignments:' takes no inline value")
                index += 1
                parsed: list[InventoryAssignment] = []
                while index < len(lines) and lines[index].startswith("  - "):
                    parsed.append(_parse_assignment(lines[index], path=path, index=index))
                    index += 1
                if not parsed:
                    raise _body_error(
                        path, index, "'assignments:' requires at least one assignment"
                    )
                assignments = tuple(parsed)
                fields[key] = ""
                continue
            fields[key] = value
            index += 1
        issue = _build_issue(identity, fields, assignments, path=path, index=index)
        for assignment in issue.assignments:
            if assignment.assignment_id in seen_assignments:
                raise _body_error(
                    path, index,
                    f"duplicate assignment identity {assignment.assignment_id!r}",
                )
            seen_assignments.add(assignment.assignment_id)
        issues.append(issue)
    result = tuple(issues)
    _validate_lineage(result, path=path)
    return result


def load_inventory(path: Path, *, verify_digest: bool = True) -> CarryForwardInventory:
    """Parse and validate one reviewed inventory Markdown file.

    ``verify_digest=False`` is only for authoring a new inventory: it returns the
    computed digest so a caller can write it into the front matter.  The import
    entry point always verifies the digest before it writes anything.
    """
    try:
        value, body = read_markdown(path, _InventoryFrontMatter)
    except ContractError as exc:
        raise CarryForwardRefused(str(exc)) from exc
    front = value
    assert isinstance(front, _InventoryFrontMatter)
    inventory = CarryForwardInventory(
        version=front.version,
        predecessor_run_id=front.predecessor_run_id,
        predecessor_candidate_sha=front.predecessor_candidate_sha,
        inventory_digest=front.inventory_digest,
        source_shas=dict(front.source_shas),
        issues=_parse_body(body, path=path),
    )
    if verify_digest:
        try:
            inventory.verify_digest()
        except CarryForwardRefused as exc:
            raise CarryForwardRefused(f"{path}: {exc}") from exc
    return inventory


def _assignment_state(assignment: InventoryAssignment) -> AssignmentState:
    # Exact historical counters are copied.  ``None`` is the committed marker
    # for legacy provenance that must be reconstructed before a new charge;
    # the importer never invents failed ordinals or an assessed high-water mark.
    return AssignmentState(
        assignment_id=assignment.assignment_id,
        repairs=assignment.repairs,
        failed_repairs=assignment.failed,
        review_rounds=assignment.rounds,
        assessed_repairs=None,
        failed_repair_ordinals=None,
    )


def _v1_state(inventory: CarryForwardInventory, run_id: str, slug: str) -> RunState:
    issues = {
        item.id: IssueState(
            id=item.id,
            worker=item.worker,
            root_issue_id=item.root,
            parent_issue_id=item.parent,
            depth=item.depth,
            status=item.status,
            owner_role=item.owner_role,
            assignments=[_assignment_state(a) for a in item.assignments],
            attempt=item.attempt,
            evidence=list(item.evidence),
            summary=item.summary,
        )
        for item in inventory.issues
    }
    return RunState(version=1, run_id=run_id, slug=slug, issues=issues)


def _verify_mapped_issue(item: InventoryIssue, issue: IssueState) -> None:
    problems: list[str] = []
    if issue.id != item.id:
        problems.append("identity")
    if issue.worker != item.worker:
        problems.append("owner")
    if issue.owner_role != item.owner_role:
        problems.append("owner_role")
    if issue.root_issue_id != item.root:
        problems.append("root")
    if issue.parent_issue_id != item.parent:
        problems.append("parent")
    if issue.depth != item.depth:
        problems.append("depth")
    if issue.status != item.lifecycle:
        problems.append("lifecycle")
    if issue.legacy_status != item.status:
        problems.append("legacy status")
    if issue.attempt != item.attempt:
        problems.append("attempt")
    if issue.summary != item.summary:
        problems.append("summary")
    if list(issue.evidence) != list(item.evidence):
        problems.append("evidence")
    if issue.reassignments != item.reassignments:
        problems.append("reassignments")
    if len(issue.assignments) != len(item.assignments):
        problems.append("assignment count")
    else:
        for assignment, expected in zip(issue.assignments, item.assignments, strict=True):
            if assignment.assignment_id != expected.assignment_id:
                problems.append(f"assignment {expected.assignment_id} identity")
            if (assignment.repairs, assignment.failed_repairs,
                    assignment.review_rounds) != (
                    expected.repairs, expected.failed, expected.rounds):
                problems.append(f"assignment {expected.assignment_id} counters")
    if problems:
        raise CarryForwardRefused(
            f"issue {item.id}: explicit migration did not preserve "
            + ", ".join(problems)
        )


def _migrated_issues(inventory: CarryForwardInventory, run_id: str,
                     slug: str) -> dict[str, IssueState]:
    v1 = _v1_state(inventory, run_id, slug)
    try:
        migrated = migrate_v1_state(
            v1,
            issue_lifecycles={item.id: item.lifecycle for item in inventory.issues},
            assignment_ids={item.id: item.assignment_ids() for item in inventory.issues},
        )
    except ContractError as exc:
        raise CarryForwardRefused(f"explicit migration refused: {exc}") from exc
    for item in inventory.issues:
        _verify_mapped_issue(item, migrated.issues[item.id])
    return dict(migrated.issues)


def _operation(state: RunState) -> dict | None:
    return state.operations.get(CARRY_FORWARD_IMPORT_OPERATION)


def _verify_operation_digest(state: RunState, inventory: CarryForwardInventory) -> dict:
    operation = _operation(state)
    if operation is None:
        raise CarryForwardRefused("carry-forward import has no durable intent")
    if operation.get("inventory_digest") != inventory.inventory_digest:
        raise CarryForwardRefused(
            "carry-forward import intent records a different inventory digest; "
            "resume with the reviewed inventory or start a new run"
        )
    status = operation.get("status")
    if status not in ("intent", "completed"):
        raise CarryForwardRefused(
            f"carry-forward import has unrecognized operation status {status!r}"
        )
    return operation


def _target_state(store: StateStore, run_id: str) -> RunState:
    try:
        run_dir = store.run_dir(run_id)
    except ValueError as exc:
        raise CarryForwardRefused(str(exc)) from exc
    if not (run_dir / "state.json").is_file():
        raise CarryForwardRefused(
            f"target run {run_id!r} does not exist; the carry-forward importer "
            "never creates a run"
        )
    try:
        state = store.load(run_id)
    except ContractError as exc:
        raise CarryForwardRefused(str(exc)) from exc
    try:
        require_operational_schema(state)
    except ContractError as exc:
        raise CarryForwardRefused(str(exc)) from exc
    return state


def _verify_target_issues(state: RunState, expected: dict[str, IssueState]) -> None:
    unknown = sorted(set(state.issues) - set(expected))
    if unknown:
        raise CarryForwardRefused(
            "target run records issues outside the reviewed inventory: "
            + ", ".join(unknown)
        )
    for identity, issue in expected.items():
        current = state.issues.get(identity)
        if current is None:
            continue
        if current.model_dump(mode="json") != issue.model_dump(mode="json"):
            raise CarryForwardRefused(
                f"target run already records issue {identity!r} with content that "
                "contradicts the reviewed inventory"
            )


def _read_predecessor(run_dir: Path, *, target_run_dir: Path,
                      expected_run_id: str) -> RunState:
    """Read predecessor state without any store write or render side effect."""
    resolved = run_dir.resolve()
    target = target_run_dir.resolve()
    if (resolved == target or resolved.is_relative_to(target)
            or target.is_relative_to(resolved)):
        raise CarryForwardRefused(
            "predecessor access must be read-only and disjoint from the target "
            "run directory"
        )
    if not resolved.is_dir():
        raise CarryForwardRefused(f"predecessor run directory does not exist: {run_dir}")
    state_path = resolved / "state.json"
    if not state_path.is_file():
        raise CarryForwardRefused(f"predecessor run has no state.json: {run_dir}")
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CarryForwardRefused(
            f"cannot read predecessor state read-only: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise CarryForwardRefused("predecessor state is not a JSON object")
    if data.get("version") != 1:
        raise CarryForwardRefused(
            "predecessor state is not version one; carry-forward import requires "
            "a version-one predecessor"
        )
    try:
        state = RunState.model_validate(data)
    except ValidationError as exc:
        raise CarryForwardRefused(
            "predecessor state is invalid: " + validation_detail(exc, RunState)
        ) from exc
    if state.run_id != expected_run_id:
        raise CarryForwardRefused(
            f"predecessor state run ID {state.run_id!r} does not match inventory "
            f"predecessor_run_id {expected_run_id!r}"
        )
    return state


def _verify_predecessor(inventory: CarryForwardInventory, predecessor: RunState) -> None:
    if predecessor.version != 1:
        raise CarryForwardRefused("predecessor state is not version one")
    inventory_issues = {item.id: item for item in inventory.issues}
    predecessor_ids = set(predecessor.issues)
    inventory_ids = set(inventory_issues)
    if predecessor_ids != inventory_ids:
        details: list[str] = []
        missing = sorted(predecessor_ids - inventory_ids)
        unknown = sorted(inventory_ids - predecessor_ids)
        if missing:
            details.append("missing predecessor issues: " + ", ".join(missing))
        if unknown:
            details.append("unknown inventory issues: " + ", ".join(unknown))
        raise CarryForwardRefused(
            "inventory does not match predecessor read-only state: " + "; ".join(details)
        )
    for identity, item in inventory_issues.items():
        current = predecessor.issues[identity]
        if current.status not in _LEGACY_STATUSES:
            raise CarryForwardRefused(
                f"predecessor issue {identity!r} has canonical status "
                f"{current.status!r}; the predecessor must be a version-one record"
            )
        checks = (
            ("owner", current.worker, item.worker),
            ("root", current.root_issue_id, item.root),
            ("parent", current.parent_issue_id, item.parent),
            ("depth", current.depth, item.depth),
            ("legacy status", current.status, item.status),
            ("attempt", current.attempt, item.attempt),
            ("summary", current.summary, item.summary),
            ("evidence", list(current.evidence), list(item.evidence)),
        )
        for label, actual, expected in checks:
            if actual != expected:
                raise CarryForwardRefused(
                    f"inventory issue {identity!r} {label} does not match "
                    "predecessor read-only state"
                )
        if current.owner_role is not None and current.owner_role != item.owner_role:
            raise CarryForwardRefused(
                f"inventory issue {identity!r} owner_role does not match "
                "predecessor read-only state"
            )
        if len(current.assignments) != len(item.assignments):
            raise CarryForwardRefused(
                f"inventory issue {identity!r} assignment count does not match "
                "predecessor read-only state"
            )
        for assignment, expected in zip(current.assignments, item.assignments, strict=True):
            if assignment.assignment_id is not None and (
                    assignment.assignment_id != expected.assignment_id):
                raise CarryForwardRefused(
                    f"inventory issue {identity!r} assignment identity does not match "
                    "predecessor read-only state"
                )
            if (assignment.repairs, assignment.failed_repairs,
                    assignment.review_rounds) != (
                    expected.repairs, expected.failed, expected.rounds):
                raise CarryForwardRefused(
                    f"inventory issue {identity!r} assignment counters do not match "
                    "predecessor read-only state"
                )


def begin_carry_forward_import(store: StateStore, state: RunState,
                               inventory: CarryForwardInventory) -> RunState:
    """Persist the import intent before any issue is written.

    Repeating an identical intent is idempotent.  A completed import returns
    unchanged; a different digest or an unrecognized status is refused.
    """
    inventory.verify_digest()
    expected = _migrated_issues(inventory, state.run_id, state.slug)
    operation = _operation(state)
    if operation is not None:
        _verify_operation_digest(state, inventory)
        return state
    _verify_target_issues(state, expected)
    return store.record_operation(state, CARRY_FORWARD_IMPORT_OPERATION, {
        "status": "intent",
        "inventory_digest": inventory.inventory_digest,
        "predecessor_run_id": inventory.predecessor_run_id,
        "predecessor_candidate_sha": inventory.predecessor_candidate_sha,
        "issue_ids": inventory.issue_ids,
        "intent_at": timestamp(),
    })


def import_carry_forward_issue(store: StateStore, state: RunState,
                               inventory: CarryForwardInventory,
                               issue_id: str) -> RunState:
    """Atomically save one issue; the audit event is a separate boundary."""
    inventory.verify_digest()
    operation = _verify_operation_digest(state, inventory)
    if operation.get("status") == "completed":
        return state
    expected = _migrated_issues(inventory, state.run_id, state.slug)
    if issue_id not in expected:
        raise CarryForwardRefused(f"inventory has no issue {issue_id!r}")
    current = state.issues.get(issue_id)
    if current is not None:
        if current.model_dump(mode="json") != expected[issue_id].model_dump(mode="json"):
            raise CarryForwardRefused(
                f"target run already records issue {issue_id!r} with content that "
                "contradicts the reviewed inventory"
            )
        return state
    audited = [
        event for event in store.events(state.run_id)
        if event.get("kind") == CARRY_FORWARD_ISSUE_EVENT
        and event.get("issue_id") == issue_id
    ]
    if audited:
        raise CarryForwardRefused(
            f"issue {issue_id!r} has audit evidence but no durable issue; "
            "the contradiction cannot be repaired by writing the issue"
        )
    state.issues[issue_id] = expected[issue_id].model_copy(deep=True)
    return store.save(state)


def audit_carry_forward_issue(store: StateStore, state: RunState,
                              inventory: CarryForwardInventory,
                              issue_id: str) -> RunState:
    """Append the stable per-issue audit event for an already-saved issue."""
    inventory.verify_digest()
    operation = _verify_operation_digest(state, inventory)
    if operation.get("status") == "completed":
        return state
    expected = _migrated_issues(inventory, state.run_id, state.slug)
    item = inventory.issue(issue_id)
    durable = store.load(state.run_id)
    current = durable.issues.get(issue_id)
    if current is None:
        raise CarryForwardRefused(
            f"cannot audit issue {issue_id!r} before it is durably imported"
        )
    if current.model_dump(mode="json") != expected[issue_id].model_dump(mode="json"):
        raise CarryForwardRefused(
            f"issue {issue_id!r} no longer matches the reviewed inventory; "
            "the audit event cannot be recorded"
        )
    matching = [
        event for event in store.events(state.run_id)
        if event.get("kind") == CARRY_FORWARD_ISSUE_EVENT
        and event.get("issue_id") == issue_id
    ]
    if len(matching) > 1:
        raise CarryForwardRefused(f"issue {issue_id!r} has multiple audit events")
    if matching:
        if matching[0].get("inventory_digest") != inventory.inventory_digest:
            raise CarryForwardRefused(
                f"issue {issue_id!r} audit event disagrees with the inventory digest"
            )
        if matching[0].get("issue_digest") != item.issue_digest:
            raise CarryForwardRefused(
                f"issue {issue_id!r} audit event disagrees with its durable record"
            )
        return state
    counters = {
        assignment.assignment_id: {
            "repairs": assignment.repairs,
            "failed": assignment.failed,
            "rounds": assignment.rounds,
        }
        for assignment in item.assignments
    }
    store.event(
        state.run_id, CARRY_FORWARD_ISSUE_EVENT,
        inventory_digest=inventory.inventory_digest,
        issue_id=issue_id,
        issue_digest=item.issue_digest,
        lifecycle=item.lifecycle,
        legacy_status=item.status,
        assignment_ids=item.assignment_ids(),
        counters=counters,
        evidence=list(item.evidence),
        dispositions=list(item.dispositions),
        uncertainties=list(item.uncertainties),
    )
    return state


def verify_carry_forward_import(store: StateStore, state: RunState,
                                inventory: CarryForwardInventory) -> RunState:
    """Verify durable counters, evidence and audit events before completion."""
    inventory.verify_digest()
    try:
        durable = store.load(state.run_id)
    except ContractError as exc:
        raise CarryForwardRefused(str(exc)) from exc
    _verify_operation_digest(durable, inventory)
    expected = _migrated_issues(inventory, durable.run_id, durable.slug)
    for item in inventory.issues:
        current = durable.issues.get(item.id)
        if current is None:
            raise CarryForwardRefused(
                f"issue {item.id!r} is not durably imported; completion is refused"
            )
        if current.model_dump(mode="json") != expected[item.id].model_dump(mode="json"):
            raise CarryForwardRefused(
                f"durable issue {item.id!r} does not match the reviewed inventory; "
                "completion is refused"
            )
        if list(current.evidence) != list(item.evidence):
            raise CarryForwardRefused(
                f"durable issue {item.id!r} evidence does not match the inventory"
            )
        for assignment, source in zip(current.assignments, item.assignments, strict=True):
            if (assignment.repairs, assignment.failed_repairs,
                    assignment.review_rounds) != (
                    source.repairs, source.failed, source.rounds):
                raise CarryForwardRefused(
                    f"durable issue {item.id!r} counters do not match the inventory"
                )
        matching = [
            event for event in store.events(durable.run_id)
            if event.get("kind") == CARRY_FORWARD_ISSUE_EVENT
            and event.get("issue_id") == item.id
        ]
        if len(matching) != 1:
            raise CarryForwardRefused(
                f"issue {item.id!r} has {len(matching)} audit events; exactly one "
                "is required before completion"
            )
        if matching[0].get("inventory_digest") != inventory.inventory_digest:
            raise CarryForwardRefused(
                f"issue {item.id!r} audit event disagrees with the inventory digest"
            )
        if matching[0].get("issue_digest") != item.issue_digest:
            raise CarryForwardRefused(
                f"issue {item.id!r} audit event disagrees with its durable record"
            )
    return durable


def _completion_events(store: StateStore, run_id: str) -> list[dict]:
    return [
        event for event in store.events(run_id)
        if event.get("kind") == CARRY_FORWARD_COMPLETED_EVENT
    ]


def _ensure_completion_event(store: StateStore, state: RunState,
                             inventory: CarryForwardInventory,
                             operation: dict) -> dict:
    matching = _completion_events(store, state.run_id)
    if len(matching) > 1:
        raise CarryForwardRefused("carry-forward import has multiple completion events")
    if matching:
        if matching[0].get("inventory_digest") != inventory.inventory_digest:
            raise CarryForwardRefused(
                "carry-forward completion event disagrees with the inventory digest"
            )
        if matching[0].get("predecessor_run_id") != inventory.predecessor_run_id:
            raise CarryForwardRefused(
                "carry-forward completion event disagrees with the predecessor run"
            )
        return matching[0]
    completed_at = operation.get("completed_at") or timestamp()
    return store.event(
        state.run_id, CARRY_FORWARD_COMPLETED_EVENT,
        inventory_digest=inventory.inventory_digest,
        predecessor_run_id=inventory.predecessor_run_id,
        predecessor_candidate_sha=inventory.predecessor_candidate_sha,
        issue_ids=inventory.issue_ids,
        audited_issue_ids=inventory.issue_ids,
        completed_at=completed_at,
    )


def record_carry_forward_completion(store: StateStore, state: RunState,
                                    inventory: CarryForwardInventory) -> RunState:
    """Verify everything and durably mark the import operation completed."""
    inventory.verify_digest()
    operation = _verify_operation_digest(state, inventory)
    if operation.get("status") == "completed":
        return state
    durable = verify_carry_forward_import(store, state, inventory)
    return store.record_operation(durable, CARRY_FORWARD_IMPORT_OPERATION, {
        "status": "completed",
        "inventory_digest": inventory.inventory_digest,
        "predecessor_run_id": inventory.predecessor_run_id,
        "predecessor_candidate_sha": inventory.predecessor_candidate_sha,
        "issue_ids": inventory.issue_ids,
        "audited_issue_ids": inventory.issue_ids,
        "completed_at": timestamp(),
    })


def emit_carry_forward_completion(store: StateStore, state: RunState,
                                  inventory: CarryForwardInventory) -> dict:
    """Emit the completion audit event for an already-completed operation."""
    inventory.verify_digest()
    operation = _verify_operation_digest(state, inventory)
    if operation.get("status") != "completed":
        raise CarryForwardRefused(
            "carry-forward completion event requires a completed operation"
        )
    return _ensure_completion_event(store, state, inventory, operation)


def complete_carry_forward_import(store: StateStore, state: RunState,
                                  inventory: CarryForwardInventory) -> RunState:
    """Record operation completion and its audit event."""
    state = record_carry_forward_completion(store, state, inventory)
    emit_carry_forward_completion(store, state, inventory)
    return state


def _completion_report(store: StateStore, state: RunState,
                       inventory: CarryForwardInventory,
                       operation: dict) -> CarryForwardReport:
    event = _ensure_completion_event(store, state, inventory, operation)
    return CarryForwardReport(
        status="completed",
        run_id=state.run_id,
        inventory_digest=inventory.inventory_digest,
        predecessor_run_id=inventory.predecessor_run_id,
        imported_issues=(),
        already_imported_issues=tuple(inventory.issue_ids),
        audit_event_ids=(),
        completion_event_id=event.get("id"),
    )


def import_carry_forward(
    store: StateStore,
    *,
    run_id: str,
    inventory_path: Path,
    predecessor_run_dir: Path | None = None,
    expected_predecessor_run_id: str | None = None,
    expected_predecessor_candidate_sha: str | None = None,
) -> CarryForwardReport:
    """Import one reviewed inventory into an existing target run.

    All validation and predecessor checks happen before the intent write.
    The predecessor directory is only read; it is never opened for writing.
    """
    inventory = load_inventory(inventory_path)
    if (expected_predecessor_run_id is not None
            and expected_predecessor_run_id != inventory.predecessor_run_id):
        raise CarryForwardRefused(
            "inventory predecessor_run_id does not match the expected predecessor run"
        )
    if (expected_predecessor_candidate_sha is not None
            and expected_predecessor_candidate_sha
            != inventory.predecessor_candidate_sha):
        raise CarryForwardRefused(
            "inventory predecessor_candidate_sha does not match the expected "
            "predecessor candidate"
        )
    if run_id == inventory.predecessor_run_id:
        raise CarryForwardRefused(
            "the target run cannot be the predecessor run; carry-forward import "
            "requires a separate target run"
        )
    state = _target_state(store, run_id)
    operation = _operation(state)
    if operation is not None:
        _verify_operation_digest(state, inventory)
        if operation.get("status") == "completed":
            return _completion_report(store, state, inventory, operation)

    if predecessor_run_dir is not None:
        predecessor = _read_predecessor(
            predecessor_run_dir,
            target_run_dir=store.run_dir(run_id),
            expected_run_id=inventory.predecessor_run_id,
        )
        _verify_predecessor(inventory, predecessor)

    expected = _migrated_issues(inventory, state.run_id, state.slug)
    _verify_target_issues(state, expected)
    already = tuple(issue_id for issue_id in inventory.issue_ids
                    if issue_id in state.issues)
    resumed = operation is not None and operation.get("status") == "intent"
    state = begin_carry_forward_import(store, state, inventory)
    imported: list[str] = []
    for item in inventory.issues:
        was_present = item.id in state.issues
        state = import_carry_forward_issue(store, state, inventory, item.id)
        if not was_present:
            imported.append(item.id)
        state = audit_carry_forward_issue(store, state, inventory, item.id)
    state = complete_carry_forward_import(store, state, inventory)
    completion = _completion_events(store, state.run_id)
    audit_ids = tuple(
        event["id"] for event in store.events(state.run_id)
        if event.get("kind") == CARRY_FORWARD_ISSUE_EVENT
    )
    return CarryForwardReport(
        status="resumed" if resumed else "imported",
        run_id=state.run_id,
        inventory_digest=inventory.inventory_digest,
        predecessor_run_id=inventory.predecessor_run_id,
        imported_issues=tuple(imported),
        already_imported_issues=already,
        audit_event_ids=audit_ids,
        completion_event_id=completion[0]["id"] if completion else None,
    )
