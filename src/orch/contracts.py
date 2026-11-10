"""Strict Markdown contracts, approval hashing and portable path rules for every stage."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

ERROR_CATEGORIES = (
    "validation",
    "approval_required",
    "infrastructure",
    "check",
    "conflict",
    "interrupted",
)

# The four canonical writable lifecycle statuses (plan section 4). The two
# legacy values ``open`` and ``resolved`` remain accepted because version-one
# run state must load and render without a silent rewrite. New transitions only
# write canonical values.
LIFECYCLE_STATUSES = ("pending", "in_repair", "finished", "escalated")
LifecycleStatus = Literal["pending", "in_repair", "finished", "escalated"]
# One status field is authoritative for every schema version. The union keeps
# historical version-one values parseable while new code writes canonical
# lifecycle values.
IssueStatus = Literal["pending", "in_repair", "finished", "escalated", "open", "resolved"]
LEGACY_STATUS_LIFECYCLE: dict[str, LifecycleStatus] = {
    "open": "pending",
    "resolved": "finished",
}
STATE_SCHEMA_VERSION = 2
SUPPORTED_STATE_SCHEMA_VERSIONS = (1, STATE_SCHEMA_VERSION)
REVIEW_APPLICATION_OPERATION = "review-application:"
EVIDENCE_CLASSIFICATIONS = ("verified", "recomputed_unbound", "unverifiable", "conflict")


class OrchestratorError(Exception):
    """Base for domain errors carrying a stable category for workflow routing."""

    category: ClassVar[str] = "validation"


class ContractError(OrchestratorError, ValueError):
    """A file or argument cannot be trusted as a protocol input."""

    category = "validation"


class ApprovalRequiredError(OrchestratorError, ValueError):
    """Approved inputs changed; the human must approve again before dispatch."""

    category = "approval_required"


class InfrastructureError(OrchestratorError, ValueError):
    """A tool, path or environment failure outside agent judgment."""

    category = "infrastructure"


class CheckError(OrchestratorError, ValueError):
    """A deterministic check failed and must return evidence to its owner."""

    category = "check"


class ConflictError(OrchestratorError, ValueError):
    """A recorded git conflict awaiting conflict resolution."""

    category = "conflict"


class InterruptedError(OrchestratorError, ValueError):
    """A pause, abort, cancellation or timeout stopped the unit of work."""

    category = "interrupted"


class LifecycleTransitionError(ContractError):
    """An issue lifecycle transition is not listed or its guard does not hold.

    Raised before any mutation, so a refused transition never leaves a partial
    status or counter change behind.
    """


class UnverifiableEvidenceError(ContractError):
    """Authoritative consumption was requested without complete provenance."""


class EvidenceConflictError(ConflictError):
    """Retained evidence contradicts itself; affected work must stop."""


def error_category(exc: BaseException) -> str:
    """Classify any raised error into the frozen category set.

    Taxonomy errors self-describe. Classes owned by other modules are mapped
    first because they import this module first and may inherit a taxonomy
    base with a different category. ``workflow.Escalation`` is a control-flow
    signal the reducer handles directly; if classified here it falls back to
    infrastructure.
    """
    import asyncio
    import builtins

    from orch import git_ops, workflow

    if isinstance(exc, git_ops.GitConflict):
        return "conflict"
    if isinstance(exc, workflow.ApprovalRequired):
        return "approval_required"
    category = getattr(exc, "category", None)
    if isinstance(category, str) and category in ERROR_CATEGORIES:
        return category
    if isinstance(exc, (asyncio.CancelledError, TimeoutError, KeyboardInterrupt,
                        builtins.InterruptedError, InterruptedError)):
        return "interrupted"
    if isinstance(exc, (OSError, RuntimeError)):
        return "infrastructure"
    if isinstance(exc, ValueError):
        return "validation"
    return "infrastructure"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ContractError(f"Duplicate or non-string YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def read_markdown(path: Path, schema: type[BaseModel]) -> tuple[BaseModel, str]:
    """Parse front matter strictly and enforce schema-specific body requirements."""
    text = path.read_text(encoding="utf-8-sig")
    match = re.fullmatch(r"---\r?\n(.*?)\r?\n---(?:\r?\n|$)(.*)", text, re.DOTALL)
    if not match:
        raise ContractError(f"{path}: expected YAML front matter")
    try:
        data = yaml.load(match[1], Loader=UniqueLoader)
        if not isinstance(data, dict):
            raise ContractError("Front matter must be a mapping")
        value = schema.model_validate(data)
    except (yaml.YAMLError, ValueError, TypeError) as exc:
        raise ContractError(f"{path}: {exc}") from exc
    body = match[2]
    for heading in REQUIRED_BODY_SECTIONS.get(schema, ()):
        if heading not in body:
            raise ContractError(f"{path}: body must include a '{heading}' section")
    return value, body


def write_markdown(path: Path, value: BaseModel, body: str = "") -> None:
    from orch.store import atomic_write
    # A feature file contains explicit overrides, not an expansion of inherited
    # global defaults. Expanding null role models/default limits would erase them.
    values = value.model_dump(mode="json", exclude_unset=isinstance(value, FeatureSpec))
    header = yaml.safe_dump(values, sort_keys=False)
    atomic_write(path, f"---\n{header}---\n{body}")


def safe_relative(value: str) -> str:
    if not value or "\\" in value or ":" in value or value.startswith("/"):
        raise ValueError(f"Expected portable repository-relative path: {value!r}")
    if any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError(f"Unsafe relative path: {value!r}")
    return value


def within(root: Path, relative: str) -> Path:
    path = root / safe_relative(relative)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ContractError(f"Path escapes {root}: {relative}")
    return path


def matches(path: str, pattern: str) -> bool:
    """Anchored portable globs; ** includes zero or more directory components."""
    safe_relative(path)
    safe_relative(pattern)
    parts = pattern.split("/")
    expression = ""
    for i, part in enumerate(parts):
        if part == "**":
            expression += ".*" if i == len(parts) - 1 else "(?:[^/]+/)*"
        else:
            expression += re.escape(part).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
            if i < len(parts) - 1:
                expression += "/"
    return re.fullmatch(expression, path) is not None


def scopes_overlap(a: str, b: str) -> bool:
    # Reject ambiguous wildcard intersections conservatively rather than permitting collisions.
    def prefix(pattern):
        result = []
        for component in pattern.casefold().split("/"):
            if "*" in component or "?" in component:
                break
            result.append(component)
        return tuple(result)
    pa, pb = prefix(a), prefix(b)
    return pa[:len(pb)] == pb or pb[:len(pa)] == pa


class Limits(StrictModel):
    max_repair_attempts: int = Field(default=3, ge=1)
    max_review_rounds: int = Field(default=3, ge=1)
    max_reassignments: int = Field(default=1, ge=0, le=1)
    max_failed_repairs_per_assignment: int = Field(default=2, ge=1)
    max_issue_depth: int = Field(default=2, ge=0, le=2)
    worker_timeout_min: float = Field(default=45, gt=0)
    max_runtime_min: float = Field(default=480, gt=0)
    max_infrastructure_retries: int = Field(default=2, ge=0)
    concurrency: int = Field(default=4, ge=1, le=4)


class ModelSpec(StrictModel):
    model: str = Field(min_length=1)
    reasoning: str | None = None


class ReplacementModel(StrictModel):
    """A replacement reuses the worker model and may override only its reasoning."""

    inherit: Literal["worker"] = "worker"
    reasoning: str | None = None


class RoleModels(StrictModel):
    planner: ModelSpec | None = None
    worker: ModelSpec | None = None
    code_reviewer: ModelSpec | None = None
    docs_reviewer: ModelSpec | None = None
    conflict_resolver: ModelSpec | None = None
    replacement: ReplacementModel = Field(default_factory=ReplacementModel)

    def resolve(self, role: str) -> ModelSpec:
        """Return the effective specification without mutating configured roles."""
        if role == "replacement":
            worker = self.worker
            if not isinstance(worker, ModelSpec):
                raise ContractError("Configure models.worker.model before dispatch")
            # Always a fresh value: callers own the resolved replacement and the
            # ordinary worker keeps its independent reasoning setting.
            override = self.replacement.reasoning
            if override is None:
                return worker.model_copy(deep=True)
            return worker.model_copy(update={"reasoning": override}, deep=True)
        value = getattr(self, role, None)
        if not isinstance(value, ModelSpec):
            raise ContractError(f"Configure models.{role}.model before dispatch")
        return value


class FeatureSpec(StrictModel):
    _runtime_settings: dict = PrivateAttr(default_factory=dict)

    version: Literal[1] = 1
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    target_project: Path
    requirement: Path
    base_branch: str = "main"
    run_mode: Literal["auto", "step"] = "auto"
    models: RoleModels = Field(default_factory=RoleModels)
    limits: Limits = Field(default_factory=Limits)
    commit_plan: bool = True
    strip_plan_on_merge: bool = False
    commit_run_history: bool = False


class WorkerSpec(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    title: str
    instruction: str
    allowed_paths: list[str] = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("instruction")
    @classmethod
    def instruction_safe(cls, value):
        return safe_relative(value)

    @field_validator("allowed_paths")
    @classmethod
    def paths_safe(cls, values):
        seen = set()
        for value in values:
            safe_relative(value)
            if scopes_overlap(value, ".plans/**"):
                raise ValueError("Worker scopes must exclude .plans and broad parent globs")
            folded = value.casefold()
            if folded in seen:
                raise ValueError(f"Duplicate or case-colliding scope: {value}")
            seen.add(folded)
        return values


class CheckSpec(StrictModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    argv: list[str] | None = None
    cmd: str | None = None
    shell: Literal["bash", "pwsh"] | None = None
    timeout_seconds: float = Field(default=300, gt=0)
    applies_to: Literal["worker", "integration", "both"] = "both"

    @model_validator(mode="after")
    def command(self):
        if bool(self.argv) == bool(self.cmd):
            raise ValueError("Specify exactly one nonempty argv or cmd")
        if self.argv and not self.argv[0].strip():
            raise ValueError("argv must start with a nonempty executable")
        if self.cmd is not None and not self.cmd.strip():
            raise ValueError("cmd must not be empty or whitespace")
        if self.cmd and not self.shell:
            raise ValueError("String commands require an explicit shell")
        if self.argv and self.shell:
            raise ValueError("Shell policy applies only to cmd string commands")
        return self


class ReviewerSpec(StrictModel):
    code: str = "reviewer.md"
    docs: str = "doc-reviewer.md"

    @field_validator("code", "docs")
    @classmethod
    def relative(cls, value):
        return safe_relative(value)


class Manifest(StrictModel):
    version: Literal[1] = 1
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    branch: str = Field(min_length=1)
    base_branch: str = Field(default="main", min_length=1)
    workers: list[WorkerSpec] = Field(min_length=1, max_length=4)
    reviewers: ReviewerSpec = Field(default_factory=ReviewerSpec)
    checks: list[CheckSpec] = Field(default_factory=list)
    limits: Limits = Field(default_factory=Limits)
    # Optional approved dependency record. When configured it participates in
    # approval hashing exactly like the other reviewed plan inputs.
    dependency_record: str | None = None

    @field_validator("dependency_record")
    @classmethod
    def dependency_record_path(cls, value):
        return safe_relative(value) if value is not None else None

    @model_validator(mode="after")
    def branch_is_never_a_protected_destination(self):
        if self.branch == self.base_branch or self.branch in {"main", "master"}:
            raise ValueError("Feature branch must differ from the base and never be main/master")
        if self.branch != f"feature/{self.slug}":
            raise ValueError("Feature branch must be feature/<slug>")
        return self

    @model_validator(mode="after")
    def graph_and_scopes(self):
        ids = [w.id for w in self.workers]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate worker IDs")
        if any(len(set(worker.depends_on)) != len(worker.depends_on) for worker in self.workers):
            raise ValueError("Duplicate worker dependency")
        check_names = [check.name.casefold() for check in self.checks]
        if len(set(check_names)) != len(check_names):
            raise ValueError("Duplicate check names")
        if self.reviewers.code.casefold() == self.reviewers.docs.casefold():
            raise ValueError("Code and documentation reviewer instructions must differ")
        graph = {w.id: w.depends_on for w in self.workers}
        seen, active = set(), set()
        def visit(node):
            if node not in graph:
                raise ValueError(f"Unknown dependency: {node}")
            if node in active:
                raise ValueError("Dependency cycle")
            if node in seen:
                return
            active.add(node)
            for dep in graph[node]:
                visit(dep)
            active.remove(node)
            seen.add(node)
        for node in graph:
            visit(node)
        for i, worker in enumerate(self.workers):
            for other in self.workers[i + 1:]:
                if any(scopes_overlap(a, b) for a in worker.allowed_paths for b in other.allowed_paths):
                    raise ValueError(f"Overlapping worker scopes: {worker.id}, {other.id}")
        return self


class CheckEvidence(StrictModel):
    name: str = Field(min_length=1)
    outcome: Literal["pass", "fail", "not_run"]
    evidence: str = Field(min_length=1)

    @field_validator("evidence")
    @classmethod
    def evidence_path(cls, value):
        return safe_relative(value)


class Handover(StrictModel):
    agent: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_-]+$")
    round: int = Field(ge=1)
    status: Literal["done", "partial", "blocked"]
    files_changed: list[str]
    docs_changed: list[str]
    checks_run: list[CheckEvidence]

    @field_validator("files_changed", "docs_changed")
    @classmethod
    def paths(cls, values):
        normalized = [safe_relative(v) for v in values]
        if any(scopes_overlap(value, ".plans/**") for value in normalized):
            raise ValueError("Worker handovers cannot report changes inside .plans")
        return normalized

    @model_validator(mode="after")
    def docs_are_declared_changes(self):
        if not set(self.docs_changed) <= set(self.files_changed):
            raise ValueError("docs_changed must be a subset of files_changed")
        return self


REQUIRED_BODY_SECTIONS: dict[type, tuple[str, ...]] = {
    # The plan requires this section so a fresh agent can consume the handover
    # without chat context; reviewers judge sufficiency, not the schema.
    Handover: ("## Interfaces and contracts",),
}


class Finding(StrictModel):
    id: str = Field(min_length=1)
    worker: str = Field(min_length=1)
    severity: Literal["blocker", "major", "minor"]
    action: Literal["rework_same_worker", "reassign", "escalate"]
    summary: str = Field(min_length=1)
    parent_issue_id: str | None = None
    root_issue_id: str = Field(min_length=1)
    related_workers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def lineage_is_sane(self):
        if self.parent_issue_id == self.id:
            raise ValueError("An issue cannot be its own parent")
        if self.root_issue_id == self.id and self.parent_issue_id:
            raise ValueError("A root issue cannot declare a parent")
        return self


class Verdict(StrictModel):
    reviewer: Literal["code", "docs"]
    result: Literal["pass", "rework", "escalate"]
    # New reviewers bind their verdict to the immutable candidate they read.
    # It stays optional so historical verdicts remain parseable; acceptance
    # requires the binding unless an audited legacy recovery verifies the
    # attempt/candidate relationship from the dispatch journal instead.
    candidate_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    findings: list[Finding] = Field(default_factory=list)
    resolved: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent(self):
        if self.result == "pass" and self.findings:
            raise ValueError("Pass cannot contain unresolved findings")
        if self.result == "rework" and not self.findings:
            raise ValueError("Rework needs actionable findings")
        ids = [f.id for f in self.findings]
        if any(not identity for identity in self.resolved):
            raise ValueError("Resolved issue IDs must not be empty")
        if len(set(ids)) != len(ids) or set(ids) & set(self.resolved):
            raise ValueError("Duplicate or inconsistently resolved findings")
        return self


class Discussion(StrictModel):
    ready: bool = Field(default=False, strict=True)
    blocking_questions: list[str] = Field(default_factory=list)


class RepairOutcome(StrictModel):
    """One actual worker repair outcome with explicit issue/assignment attribution.

    Review rounds and unsuccessful-repair counters are derived from these
    records. ``initial_work`` is ordinal zero and never consumes a repair
    allowance. An outcome is charged at most once; replaying the same review
    therefore returns the retained record instead of adding another charge.
    """

    assignment_id: str = Field(min_length=1)
    repair_ordinal: int = Field(ge=0)
    outcome: Literal["initial_work", "unsuccessful", "successful"]
    review_transaction: str = Field(min_length=1)
    reviewer_role: Literal["code", "docs"]
    charged_failed_repair: bool = False
    charged_review_round: bool = False
    evidence: list[str] = Field(default_factory=list)
    recorded_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class AssignmentState(StrictModel):
    # Stable assignment identity; ``None`` is a version-one record whose
    # identity must be established by explicit migration before attribution.
    assignment_id: str | None = None
    repairs: int = Field(default=0, ge=0)
    failed_repairs: int = Field(default=0, ge=0)
    review_rounds: int = Field(default=0, ge=0)
    # Highest repair ordinal whose outcome has already been assessed by a
    # reviewer. ``None`` marks a legacy record written before this provenance
    # existed; routing normalises it from the retained counters instead of
    # charging the same repair again. The high-water mark makes a failure
    # charge idempotent across revalidation, replay and approval changes.
    assessed_repairs: int | None = Field(default=None, ge=0)
    # Exact actual-repair ordinals charged as failed. ``None`` denotes legacy
    # state that must be reconstructed from retained review transactions;
    # newly written assignments use a concrete list, including an empty one.
    failed_repair_ordinals: list[int] | None = None
    # Separately retained actual outcomes. Counters remain the routing fast
    # path; this history proves the exact attribution when a review is replayed.
    outcomes: list[RepairOutcome] = Field(default_factory=list)
    started_at: str | None = None
    closed_at: str | None = None
    reason: str | None = None


class LifecycleTransition(StrictModel):
    """One committed lifecycle change with its validated guard evidence."""

    issue_id: str
    from_status: LifecycleStatus
    to_status: LifecycleStatus
    guard: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    recorded_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class IssueState(StrictModel):
    id: str
    worker: str
    root_issue_id: str
    parent_issue_id: str | None = None
    depth: int = Field(default=0, ge=0)
    # Canonical lifecycle values are pending/in_repair/finished/escalated.
    # ``open`` and ``resolved`` are version-one historical values retained for
    # loading and rendering; transitions never write them.
    status: IssueStatus = "open"
    # Original version-one status retained by an explicit migration so
    # historical rendering never pretends the record was always canonical.
    legacy_status: str | None = None
    assignments: list[AssignmentState] = Field(
        default_factory=lambda: [AssignmentState(failed_repair_ordinals=[])])
    attempt: int = Field(default=1, ge=1)
    evidence: list[str] = Field(default_factory=list)
    summary: str = ""
    lifecycle_history: list[LifecycleTransition] = Field(default_factory=list)
    # Reviewer ownership is recorded when a finding is accepted, not inferred
    # from an issue-name prefix. ``system`` covers workflow-created check issues.
    # ``None`` is a legacy record that must be resolved from durable verdict
    # evidence before a reviewer may report or resolve it.
    owner_role: Literal["code", "docs", "system"] | None = None

    @property
    def reassignments(self) -> int:
        """Issue-level reassignment count; maximum one per issue (Q6)."""
        return len(self.assignments) - 1

    @property
    def has_repair_activity(self) -> bool:
        """Whether any assignment records repair, failure or review activity."""
        return any(assignment.repairs or assignment.failed_repairs
                   or assignment.review_rounds for assignment in self.assignments)

    @property
    def lifecycle(self) -> str:
        """Display view: canonical status, mapped historical value, or ambiguity.

        A version-one ``open`` record with repair activity cannot be classified
        as pending or in_repair from counters alone, so it renders ``ambiguous``
        rather than guessing.
        """
        if self.status in LIFECYCLE_STATUSES:
            return self.status
        if self.status == "resolved":
            return "finished"
        if self.status == "escalated":
            return "escalated"
        return "ambiguous" if self.has_repair_activity else "pending"

    def current_lifecycle(self, explicit: LifecycleStatus | None = None) -> LifecycleStatus:
        """Canonical current status, refusing an ambiguous legacy open record."""
        if explicit is not None:
            return explicit
        if self.status in LIFECYCLE_STATUSES:
            return self.status  # type: ignore[return-value]
        if self.status == "resolved":
            return "finished"
        if self.status == "escalated":
            return "escalated"
        if self.has_repair_activity:
            raise ContractError(
                f"Issue {self.id} has a legacy open status with repair activity; "
                "pending versus in_repair is ambiguous and cannot be inferred from "
                "counters. Supply an explicit compatible migration mapping."
            )
        return "pending"


class ReviewResult(StrictModel):
    """One accepted reviewer-role result bound to candidate and input evidence."""

    role: Literal["code", "docs"]
    candidate_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_id: str = Field(min_length=1)
    verdict_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict_path: str = Field(min_length=1)
    result: Literal["pass", "rework"]
    findings: list[str] = Field(default_factory=list)
    resolved: list[str] = Field(default_factory=list)
    affected_workers: list[str] = Field(default_factory=list)
    binding: Literal["candidate-sha", "legacy-attempt"] = "candidate-sha"
    accepted_at: str = Field(min_length=1)
    transaction: str = Field(min_length=1)
    escalation: str | None = None


class TransitionGuard(StrictModel):
    """Explicit facts a caller must establish before a lifecycle transition.

    The model is intentionally inert data: no field implies another, so a
    caller cannot request a transition by asserting a generic "ready" flag.
    """

    identity_valid: bool = False
    owner_lineage_valid: bool = False
    dispatch_intent: bool = False
    dependencies_ready: bool = False
    allowance_available: bool = False
    process_ownership_known: bool = False
    subsequent_attempt: bool = False
    permitted_reassignment: bool = False
    reviewer_acceptance: bool = False
    reopening_valid: bool = False
    historical_attribution: bool = False
    remaining_work: bool = False
    stop_boundary: bool = False
    attribution_unsafe: bool = False
    explicit_escalation: bool = False
    allowance_exhausted: bool = False
    depth_stop: bool = False
    stop_rule_applicable: bool = False
    scoped_human_acceptance: bool = False
    owner_disposition: bool = False
    acceptance_evidence: bool = False
    human_continuation: bool = False
    continuation_scope: bool = False
    continuation_allowance: bool = False
    no_change_evidence: bool = False
    # Workflow-owned check issues are accepted only by deterministic check
    # evidence, never by a code/docs reviewer claiming another owner's issue.
    system_check_identity: bool = False
    system_check_evidence: bool = False


# Each rule is an alternative set of facts that all must hold. Groups are
# alternatives, so a transition with two groups may be satisfied by either.
TransitionRules = dict[tuple[LifecycleStatus, LifecycleStatus], tuple[tuple[str, ...], ...]]
TRANSITION_GUARDS: TransitionRules = {
    ("pending", "in_repair"): ((
        "identity_valid", "owner_lineage_valid", "dispatch_intent",
        "dependencies_ready", "allowance_available", "process_ownership_known",
    ),),
    ("in_repair", "in_repair"): (("subsequent_attempt",), ("permitted_reassignment",)),
    ("in_repair", "finished"): (
        ("reviewer_acceptance",),
        ("scoped_human_acceptance", "owner_disposition", "acceptance_evidence"),
    ),
    ("finished", "pending"): ((
        "reopening_valid", "historical_attribution", "remaining_work",
    ),),
    ("finished", "escalated"): (
        ("reopening_valid", "stop_boundary"),
        ("reopening_valid", "attribution_unsafe"),
    ),
    ("pending", "escalated"): (
        ("explicit_escalation",),
        ("allowance_exhausted",),
        ("depth_stop",),
        ("stop_rule_applicable",),
    ),
    ("in_repair", "escalated"): (
        ("explicit_escalation",),
        ("allowance_exhausted",),
        ("depth_stop",),
        ("stop_rule_applicable",),
    ),
    ("pending", "finished"): (
        ("scoped_human_acceptance", "owner_disposition", "acceptance_evidence"),
    ),
    ("escalated", "finished"): (
        ("scoped_human_acceptance", "owner_disposition", "acceptance_evidence"),
    ),
    ("escalated", "pending"): ((
        "human_continuation", "continuation_scope", "continuation_allowance",
    ),),
}


def _satisfied_guards(guard: TransitionGuard, rules: tuple[tuple[str, ...], ...]) -> list[str]:
    matches = []
    for rule in rules:
        if all(getattr(guard, name) for name in rule):
            matches.append("+".join(rule))
    return matches


def apply_lifecycle_transition(issue: IssueState, target: LifecycleStatus,
                               guard: TransitionGuard, *,
                               evidence: list[str] | None = None) -> LifecycleTransition:
    """Validate and commit exactly one listed lifecycle transition.

    The source status is read canonically: a legacy ``open`` record with repair
    activity is ambiguous and must be given an explicit mapping instead of
    being guessed. Every unlisted transition and unmet guard is refused before
    mutation. Counters, assignments and review history are never touched.
    """
    current = issue.current_lifecycle()
    if target not in LIFECYCLE_STATUSES:
        raise LifecycleTransitionError(f"Unknown lifecycle target: {target!r}")
    rules = TRANSITION_GUARDS.get((current, target))
    if (issue.owner_role == "system" and target == "finished"
            and current in ("pending", "in_repair")):
        # Check-owned issues are finished by deterministic check evidence;
        # this does not authorize a code/docs reviewer to resolve them.
        system_rule = ("system_check_identity", "system_check_evidence")
        rules = (rules or ()) + (system_rule,)
    if current == target:
        matches = _satisfied_guards(guard, rules or ())
        if matches:
            rule_label = matches[0]
        elif guard.no_change_evidence:
            rule_label = "same_status_no_change"
        else:
            raise LifecycleTransitionError(
                f"Issue {issue.id}: same-status entry {current} requires a listed "
                "same-status justification or validated application evidence and a "
                "no-change decision"
            )
    else:
        if rules is None:
            raise LifecycleTransitionError(
                f"Issue {issue.id}: lifecycle transition {current} -> {target} is not listed"
            )
        matches = _satisfied_guards(guard, rules)
        if not matches:
            required = " or ".join("+".join(rule) for rule in rules)
            raise LifecycleTransitionError(
                f"Issue {issue.id}: lifecycle transition {current} -> {target} requires {required}"
            )
        rule_label = matches[0]
    record = LifecycleTransition(
        issue_id=issue.id, from_status=current, to_status=target, guard=rule_label,
        evidence=list(evidence or []),
    )
    issue.status = target
    issue.lifecycle_history.append(record)
    return record


SYSTEM_ISSUE_SCOPE = ("check-*", "post-check-*", "integration-check-*")


def create_pending_issue(identity: str, worker: str, *, root_issue_id: str,
                         parent_issue_id: str | None = None, depth: int = 0,
                         owner_role: Literal["code", "docs", "system"],
                         summary: str = "", evidence: list[str] | None = None) -> IssueState:
    """Create one validated new issue in the canonical pending status.

    Creation is the ``new issue -> pending`` transition: it establishes a
    stable identity, owner and lineage and infers no dispatch. Callers must
    still pass the dispatch guard before moving to in_repair.
    """
    if not identity.strip() or not worker.strip() or not root_issue_id.strip():
        raise ContractError("A new issue requires stable identity, owner and root lineage")
    if parent_issue_id == identity:
        raise ContractError("An issue cannot be its own parent")
    if parent_issue_id is None and root_issue_id != identity:
        raise ContractError("A root issue must name itself as its root")
    if parent_issue_id is not None and root_issue_id == identity:
        raise ContractError("A derived issue cannot be its own root")
    if depth < 0:
        raise ContractError("Issue depth must not be negative")
    return IssueState(
        id=identity, worker=worker, root_issue_id=root_issue_id,
        parent_issue_id=parent_issue_id, depth=depth, status="pending",
        owner_role=owner_role, summary=summary, evidence=list(evidence or []),
        assignments=[AssignmentState(failed_repair_ordinals=[])],
    )


def create_system_issue(identity: str, worker: str, *, root_issue_id: str,
                        summary: str, evidence: list[str]) -> IssueState:
    """Create one workflow-owned check issue in the canonical pending status.

    Only the deterministic workflow owns these identities. The ownership value
    is what prevents a code or docs reviewer from later reporting or resolving
    the issue through the ordinary reviewer path.
    """
    if not evidence:
        raise ContractError(f"System issue {identity} requires deterministic check evidence")
    if not any(matches(identity, pattern) for pattern in SYSTEM_ISSUE_SCOPE):
        raise ContractError(
            f"System issue identity {identity!r} is not one of the workflow-owned check names"
        )
    return create_pending_issue(
        identity, worker, root_issue_id=root_issue_id, owner_role="system",
        summary=summary, evidence=evidence,
    )


def finish_system_issue(issue: IssueState, *, check_evidence: list[str]) -> LifecycleTransition:
    """Finish a workflow-owned check issue from deterministic check evidence."""
    if issue.owner_role != "system":
        raise LifecycleTransitionError(
            f"Issue {issue.id}: only a workflow-owned system issue may use the check path"
        )
    if not check_evidence:
        raise LifecycleTransitionError(
            f"Issue {issue.id}: finishing a check issue requires check evidence"
        )
    return apply_lifecycle_transition(
        issue, "finished",
        TransitionGuard(system_check_identity=True, system_check_evidence=True),
        evidence=list(check_evidence),
    )


class HumanDisposition(StrictModel):
    """An explicit human control action recorded with its required evidence.

    This is the only surface for escalated continuation and scoped acceptance
    or deferral. It is invoked by the workflow on behalf of a recorded human
    decision; reviewers cannot construct one implicitly.
    """

    kind: Literal["continuation", "scoped_acceptance", "deferral"]
    issue_id: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    allowance_disposition: str = Field(min_length=1)
    dependencies: list[str] = Field(default_factory=list)
    deferred_stage: str | None = None

    @model_validator(mode="after")
    def deferral_is_labelled(self):
        if self.kind == "deferral" and not self.deferred_stage:
            raise ValueError("A deferral must name the approved stage it is deferred for")
        return self


def apply_human_disposition(issue: IssueState,
                            disposition: HumanDisposition) -> LifecycleTransition:
    """Apply one recorded human continuation or scoped acceptance/deferral."""
    if disposition.issue_id != issue.id:
        raise LifecycleTransitionError(
            f"Human disposition for {disposition.issue_id} cannot apply to {issue.id}"
        )
    if disposition.kind == "continuation":
        guard = TransitionGuard(
            human_continuation=True, continuation_scope=True, continuation_allowance=True,
        )
        evidence = [*disposition.evidence, f"scope:{disposition.scope}",
                    f"allowance:{disposition.allowance_disposition}"]
        if disposition.dependencies:
            evidence.extend(f"dependency:{item}" for item in disposition.dependencies)
        return apply_lifecycle_transition(issue, "pending", guard, evidence=evidence)
    guard = TransitionGuard(
        scoped_human_acceptance=True, owner_disposition=True, acceptance_evidence=True,
    )
    evidence = [*disposition.evidence, f"scope:{disposition.scope}",
                f"allowance:{disposition.allowance_disposition}"]
    if disposition.deferred_stage:
        evidence.append(f"deferred-stage:{disposition.deferred_stage}")
    return apply_lifecycle_transition(issue, "finished", guard, evidence=evidence)


class ActivityRecord(StrictModel):
    """Real dispatch/check/review/process activity, separate from counters."""

    activity_id: str = Field(min_length=1)
    kind: Literal["dispatch", "check", "review", "repair", "process", "lifecycle"]
    issue_id: str | None = None
    assignment_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    process_ownership: Literal["known", "uncertain"] = "known"
    started_at: str | None = None
    finished_at: str | None = None
    outcome: str | None = None
    evidence: list[str] = Field(default_factory=list)


class RepairOutcomeCharge(StrictModel):
    """The accounting result of one review entry, retained with provenance."""

    issue: str
    assignment_index: int = Field(ge=0)
    assignment_id: str
    repair_ordinal: int = Field(ge=0)
    assessment: Literal[
        "initial_work", "new_failure", "superseding_failure",
        "already_charged", "already_assessed", "resolved",
    ]
    failed_repair_charged: bool
    review_round_charged: bool
    reviewer_role: Literal["code", "docs"]
    review_transaction: str
    provenance_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewerProvenance(StrictModel):
    """Mandatory reviewer identity and binding for any authoritative decision."""

    reviewer_role: Literal["code", "docs"]
    attempt_id: str = Field(min_length=1)
    candidate_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    verdict_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict_path: str = Field(min_length=1)
    transaction: str = Field(min_length=1)
    ownership_valid: bool = False
    dispatch_evidence: bool = False
    completion_evidence: bool = False

    @classmethod
    def from_review_result(cls, result: ReviewResult) -> ReviewerProvenance:
        return cls(
            reviewer_role=result.role,
            attempt_id=result.attempt_id,
            candidate_sha=result.candidate_sha,
            verdict_sha256=result.verdict_sha256,
            evidence_digest=result.evidence_digest,
            verdict_path=result.verdict_path,
            transaction=result.transaction,
            ownership_valid=True,
            dispatch_evidence=True,
            completion_evidence=True,
        )

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


def _validate_repair_counters(identity: str, assignment: AssignmentState) -> None:
    """Refuse internally contradictory counters before a charge is applied."""
    if assignment.failed_repairs > assignment.repairs:
        raise ContractError(
            f"Issue {identity} records {assignment.failed_repairs} failed repairs for "
            f"{assignment.repairs} repair attempt(s); counters are inconsistent"
        )
    charged = assignment.failed_repair_ordinals
    if (charged is not None
            and (charged != sorted(set(charged))
                 or any(ordinal <= 0 or ordinal > assignment.repairs for ordinal in charged)
                 or len(charged) != assignment.failed_repairs)):
        raise ContractError(
            f"Issue {identity} has contradictory failed-repair ordinal provenance"
        )
    mark = assignment.assessed_repairs
    if mark is not None:
        if mark > assignment.repairs:
            raise ContractError(
                f"Issue {identity} assessed repair {mark} but only {assignment.repairs} "
                "attempt(s) exist"
            )
        if assignment.failed_repairs > mark:
            raise ContractError(
                f"Issue {identity} records {assignment.failed_repairs} failure(s) but only "
                f"repair outcome {mark} as assessed"
            )
        if charged and max(charged) > mark:
            raise ContractError(
                f"Issue {identity} records a failed ordinal above assessed outcome {mark}"
            )


def _require_provenance(provenance: ReviewerProvenance | None, identity: str) -> ReviewerProvenance:
    if provenance is None:
        raise UnverifiableEvidenceError(
            f"Issue {identity}: no reviewer provenance was supplied; authoritative "
            "accounting is refused and there is no weaker caller mode"
        )
    if not provenance.ownership_valid:
        raise UnverifiableEvidenceError(
            f"Issue {identity}: reviewer ownership has not been established"
        )
    if not (provenance.dispatch_evidence and provenance.completion_evidence):
        raise UnverifiableEvidenceError(
            f"Issue {identity}: dispatch and completion evidence is required for "
            "authoritative accounting"
        )
    return provenance


def assess_repair_outcome(issue: IssueState, identity: str,
                          provenance: ReviewerProvenance | None, *,
                          assignment_index: int | None = None,
                          resolved: bool = False) -> RepairOutcomeCharge:
    """Apply the once-per-repair accounting decision with mandatory provenance.

    ``resolved`` marks a passing-review disposition instead of a finding. An
    explicit older ``assignment_index`` charges the assignment that produced the
    defective repair; when the ordinal exists on more than one assignment and
    no index is supplied, attribution is ambiguous and refused before mutation.
    """
    provenance = _require_provenance(provenance, identity)
    if provenance.reviewer_role not in ("code", "docs"):
        raise UnverifiableEvidenceError(f"Issue {identity}: unknown reviewer role")
    if not issue.assignments:
        raise ContractError(f"Issue {identity} has no assignments")
    if assignment_index is None:
        positions = [index for index, assignment in enumerate(issue.assignments)
                     if assignment.repairs == max(a.repairs for a in issue.assignments)]
        if len(positions) != 1:
            raise ContractError(
                f"Issue {identity}: repair outcome attribution is ambiguous across "
                f"assignments {positions}; supply the exact assignment index"
            )
        assignment_index = positions[0]
    if assignment_index < 0 or assignment_index >= len(issue.assignments):
        raise ContractError(f"Issue {identity}: unknown assignment index {assignment_index}")
    assignment = issue.assignments[assignment_index]
    assignment_id = assignment.assignment_id or f"{identity}:{assignment_index}"
    _validate_repair_counters(identity, assignment)
    if assignment.failed_repair_ordinals is None:
        if (assignment.failed_repairs or assignment.review_rounds
                or issue.status in ("resolved", "finished")):
            raise ContractError(
                f"Issue {identity}: counters alone cannot prove which repair outcome "
                "was assessed; explicit migration or retained review evidence is required"
            )
        assignment.failed_repair_ordinals = []
    ordinal = assignment.repairs
    if resolved:
        new_outcome = (assignment.assessed_repairs is None
                       or ordinal > assignment.assessed_repairs)
        round_charged = issue.status in ("open", "pending", "in_repair") and new_outcome
        if round_charged:
            assignment.review_rounds += 1
        assignment.assessed_repairs = max(assignment.assessed_repairs or 0, ordinal)
        charge = RepairOutcomeCharge(
            issue=identity, assignment_index=assignment_index, assignment_id=assignment_id,
            repair_ordinal=ordinal, assessment="resolved", failed_repair_charged=False,
            review_round_charged=round_charged, reviewer_role=provenance.reviewer_role,
            review_transaction=provenance.transaction,
            provenance_digest=provenance.digest,
        )
        _record_outcome(issue, assignment, charge, provenance, resolved=True)
        return charge
    if ordinal == 0:
        if assignment.assessed_repairs is None:
            assignment.assessed_repairs = 0
        charge = RepairOutcomeCharge(
            issue=identity, assignment_index=assignment_index, assignment_id=assignment_id,
            repair_ordinal=0, assessment="initial_work", failed_repair_charged=False,
            review_round_charged=False, reviewer_role=provenance.reviewer_role,
            review_transaction=provenance.transaction,
            provenance_digest=provenance.digest,
        )
        _record_outcome(issue, assignment, charge, provenance)
        return charge
    if ordinal in assignment.failed_repair_ordinals:
        charge = RepairOutcomeCharge(
            issue=identity, assignment_index=assignment_index, assignment_id=assignment_id,
            repair_ordinal=ordinal, assessment="already_charged",
            failed_repair_charged=False, review_round_charged=False,
            reviewer_role=provenance.reviewer_role,
            review_transaction=provenance.transaction,
            provenance_digest=provenance.digest,
        )
        _record_outcome(issue, assignment, charge, provenance)
        return charge
    if issue.status in ("resolved", "finished") and assignment.assessed_repairs == ordinal:
        assignment.failed_repairs += 1
        assignment.failed_repair_ordinals.append(ordinal)
        charge = RepairOutcomeCharge(
            issue=identity, assignment_index=assignment_index, assignment_id=assignment_id,
            repair_ordinal=ordinal, assessment="superseding_failure",
            failed_repair_charged=True, review_round_charged=False,
            reviewer_role=provenance.reviewer_role,
            review_transaction=provenance.transaction,
            provenance_digest=provenance.digest,
        )
        _record_outcome(issue, assignment, charge, provenance)
        return charge
    if assignment.assessed_repairs is None or ordinal > assignment.assessed_repairs:
        assignment.assessed_repairs = ordinal
        assignment.failed_repairs += 1
        assignment.failed_repair_ordinals.append(ordinal)
        assignment.review_rounds += 1
        charge = RepairOutcomeCharge(
            issue=identity, assignment_index=assignment_index, assignment_id=assignment_id,
            repair_ordinal=ordinal, assessment="new_failure", failed_repair_charged=True,
            review_round_charged=True, reviewer_role=provenance.reviewer_role,
            review_transaction=provenance.transaction,
            provenance_digest=provenance.digest,
        )
        _record_outcome(issue, assignment, charge, provenance)
        return charge
    charge = RepairOutcomeCharge(
        issue=identity, assignment_index=assignment_index, assignment_id=assignment_id,
        repair_ordinal=ordinal, assessment="already_assessed",
        failed_repair_charged=False, review_round_charged=False,
        reviewer_role=provenance.reviewer_role,
        review_transaction=provenance.transaction,
        provenance_digest=provenance.digest,
    )
    _record_outcome(issue, assignment, charge, provenance)
    return charge


def _record_outcome(issue: IssueState, assignment: AssignmentState,
                    charge: RepairOutcomeCharge, provenance: ReviewerProvenance,
                    *, resolved: bool = False) -> RepairOutcome:
    expected = ("successful" if resolved else
                "initial_work" if charge.repair_ordinal == 0 else "unsuccessful")
    for existing in assignment.outcomes:
        if (existing.assignment_id == charge.assignment_id
                and existing.repair_ordinal == charge.repair_ordinal):
            if existing.outcome != expected:
                raise EvidenceConflictError(
                    f"Issue {issue.id} repair outcome {charge.repair_ordinal} is recorded "
                    f"as {existing.outcome}, contradicting {expected}"
                )
            # A duplicate or replayed review of the same outcome never creates a
            # second worker outcome or allowance charge.
            return existing
    outcome = RepairOutcome(
        assignment_id=charge.assignment_id, repair_ordinal=charge.repair_ordinal,
        outcome=expected,
        review_transaction=provenance.transaction,
        reviewer_role=provenance.reviewer_role,
        charged_failed_repair=charge.failed_repair_charged,
        charged_review_round=charge.review_round_charged,
        evidence=[provenance.verdict_path],
    )
    assignment.outcomes.append(outcome)
    return outcome


ApplicationEntryState = Literal["not_applied", "applied"]


class ReviewEntry(StrictModel):
    """One deterministic entry from a review sheet and its application state."""

    entry_id: str = Field(min_length=1)
    order_index: int = Field(ge=0)
    kind: Literal["finding", "resolution"]
    issue_id: str = Field(min_length=1)
    issue_before: dict = Field(default_factory=dict)
    proposed_decision: dict = Field(default_factory=dict)
    state: ApplicationEntryState = "not_applied"
    decision: dict | None = None
    applied_at: str | None = None

    @model_validator(mode="after")
    def disposition_is_consistent(self):
        if self.state == "applied" and self.decision is None:
            raise ValueError("An applied entry must retain its explicit decision")
        if self.state == "not_applied" and self.decision is not None:
            raise ValueError("A not_applied entry must not carry a committed decision")
        return self


class ReviewApplication(StrictModel):
    """Durable per-review application record with deterministic entry order."""

    application_id: str = Field(min_length=1)
    transaction: str = Field(min_length=1)
    reviewer_role: Literal["code", "docs"]
    attempt_id: str = Field(min_length=1)
    candidate_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict_path: str = Field(min_length=1)
    approved_inputs: dict[str, str] = Field(default_factory=dict)
    entries: list[ReviewEntry] = Field(default_factory=list)
    intent_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None
    complete: bool = False

    @model_validator(mode="after")
    def entries_are_ordered_and_unique(self):
        if [entry.order_index for entry in self.entries] != list(range(len(self.entries))):
            raise ValueError("Review entries must be contiguous and deterministically ordered")
        ids = [entry.entry_id for entry in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate review entry identity")
        if self.complete and any(entry.state != "applied" for entry in self.entries):
            raise ValueError("Completion requires every entry to be applied")
        return self

    @property
    def next_entry_index(self) -> int:
        for index, entry in enumerate(self.entries):
            if entry.state == "not_applied":
                return index
        return len(self.entries)

    @property
    def applied_entry_ids(self) -> list[str]:
        return [entry.entry_id for entry in self.entries if entry.state == "applied"]

    @property
    def fully_applied(self) -> bool:
        return all(entry.state == "applied" for entry in self.entries)


def build_review_application(review: ReviewResult, verdict: Verdict,
                             issues: dict[str, IssueState], *,
                             approved_inputs: dict[str, str] | None = None,
                             proposed_decisions: dict[str, dict] | None = None,
                             intent_at: str | None = None) -> ReviewApplication:
    """Build the deterministic application record for one accepted review.

    Order is the verdict's finding order followed by its resolution order. The
    identity is stable across replay and does not depend on counters or on which
    entries have already been applied.
    """
    if review.role != verdict.reviewer:
        raise ContractError(
            f"Review role {review.role!r} does not match verdict reviewer {verdict.reviewer!r}"
        )
    if set(review.findings) != {finding.id for finding in verdict.findings}:
        raise ContractError("Review result findings do not match the retained verdict")
    if sorted(review.resolved) != sorted(verdict.resolved):
        raise ContractError("Review result resolutions do not match the retained verdict")
    entries: list[ReviewEntry] = []
    proposed = proposed_decisions or {}
    for kind, identity in ([("finding", finding.id) for finding in verdict.findings]
                           + [("resolution", identity) for identity in verdict.resolved]):
        issue = issues.get(identity)
        entry_id = f"{kind}:{identity}"
        entries.append(ReviewEntry(
            entry_id=entry_id,
            order_index=len(entries),
            kind=kind,
            issue_id=identity,
            issue_before=(issue.model_dump(mode="json") if issue is not None else {}),
            proposed_decision=proposed.get(entry_id, {"kind": kind, "issue": identity}),
        ))
    transaction = review.transaction
    application_id = (transaction.replace("review-result", "review-application", 1)
                      if transaction.startswith("review-result")
                      else f"review-application:{transaction}")
    return ReviewApplication(
        application_id=application_id,
        transaction=transaction,
        reviewer_role=review.role,
        attempt_id=review.attempt_id,
        candidate_sha=review.candidate_sha,
        evidence_digest=review.evidence_digest,
        verdict_sha256=review.verdict_sha256,
        verdict_path=review.verdict_path,
        approved_inputs=dict(approved_inputs or {}),
        entries=entries,
        intent_at=intent_at or datetime.now(UTC).isoformat(),
    )


EvidenceClassification = Literal["verified", "recomputed_unbound", "unverifiable", "conflict"]


class EvidenceAssessment(StrictModel):
    """Deterministic classification of review evidence for one consumer stage."""

    operation: str
    application_id: str
    classification: EvidenceClassification
    authoritative: bool
    missing_inputs: list[str] = Field(default_factory=list)
    missing_digests: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    provenance: ReviewerProvenance | None = None


_STAGE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "acceptance": (
        "review_result", "candidate_binding", "ownership", "verdict_identity",
        "accounting_intent", "applied_decisions", "acceptance_events", "completion",
    ),
    "reconstruction": (
        "review_result", "candidate_binding", "verdict_identity", "applied_decisions",
        "acceptance_events", "completion",
    ),
    "replay": (
        "candidate_binding", "verdict_identity", "accounting_intent", "applied_decisions",
    ),
    "gap_check": (
        "candidate_binding", "verdict_identity", "accounting_intent", "applied_decisions",
    ),
    "recovery": (
        "review_result", "candidate_binding", "ownership", "verdict_identity",
        "accounting_intent", "applied_decisions", "acceptance_events", "completion",
    ),
}


def classify_review_evidence(state: RunState, application_id: str, *,
                             operation: str,
                             events: list[dict] | None = None) -> EvidenceAssessment:
    """Classify review evidence for exactly one consumer operation.

    ``verified`` is the only classification permitted for authoritative use.
    ``recomputed_unbound`` is diagnostic: it arises when the retained
    application is internally consistent but the original candidate/verdict
    binding is absent. Missing inputs and missing digests are reported
    separately because they are different historical facts. Any contradiction
    is ``conflict`` and stops affected work.
    """
    if operation not in _STAGE_REQUIREMENTS:
        raise ContractError(f"Unknown evidence consumer operation: {operation!r}")
    app = state.review_applications.get(application_id)
    if app is None:
        return EvidenceAssessment(
            operation=operation, application_id=application_id,
            classification="unverifiable", authoritative=False,
            missing_inputs=["review application"],
        )
    required = set(_STAGE_REQUIREMENTS[operation])
    missing_inputs: list[str] = []
    missing_digests: list[str] = []
    conflicts: list[str] = []
    accepted = state.review_results.get(app.transaction)
    if accepted is None:
        for candidate in state.review_results.values():
            if (candidate.role == app.reviewer_role
                    and candidate.attempt_id == app.attempt_id):
                accepted = candidate
                break
    ownership_ok = True
    ownership_missing = False
    for entry in app.entries:
        issue = state.issues.get(entry.issue_id)
        owner = (issue.owner_role if issue is not None
                 else entry.issue_before.get("owner_role"))
        if owner is None:
            ownership_missing = True
        elif owner != app.reviewer_role:
            ownership_ok = False
            conflicts.append(f"{entry.issue_id} is owned by {owner}, not {app.reviewer_role}")
    if accepted is not None:
        if accepted.role != app.reviewer_role:
            conflicts.append("accepted review result role disagrees with the application")
        if accepted.candidate_sha != app.candidate_sha:
            conflicts.append("accepted review result candidate binding disagrees")
        if accepted.verdict_sha256 != app.verdict_sha256:
            conflicts.append("accepted review result verdict digest disagrees")
    if "review_result" in required and accepted is None:
        missing_inputs.append("accepted review result")
    if "candidate_binding" in required and accepted is None:
        # The application retains a candidate, but no accepted result binds it.
        missing_inputs.append("original candidate binding")
    if "ownership" in required and ownership_missing:
        missing_inputs.append("validated issue ownership")
    if not app.verdict_sha256:
        missing_digests.append("verdict digest")
    if not app.evidence_digest:
        missing_digests.append("approved-input digest")
    if "verdict_identity" in required and not app.verdict_path:
        missing_inputs.append("verdict identity")
    if "accounting_intent" in required and any(
            not entry.proposed_decision for entry in app.entries):
        missing_inputs.append("accounting intent for every entry")
    audits = _review_application_events(events or [], app.application_id)
    audits_by_entry = {event.get("entry_id"): event for event in audits}
    if "applied_decisions" in required:
        for entry in app.entries:
            if entry.state == "applied" and entry.decision is None:
                conflicts.append(f"applied entry {entry.entry_id} has no decision")
            if entry.state == "not_applied" and entry.entry_id in audits_by_entry:
                conflicts.append(f"entry {entry.entry_id} has audit evidence but is not applied")
            if entry.state == "applied" and entry.entry_id in audits_by_entry:
                recorded = audits_by_entry[entry.entry_id].get("decision_digest")
                if recorded != _digest(entry.decision):
                    conflicts.append(f"entry {entry.entry_id} audit decision disagrees")
    if "acceptance_events" in required:
        acceptance = [event for event in (events or [])
                      if event.get("kind") == "review_result_accepted"
                      and event.get("transaction") == app.transaction]
        if not acceptance:
            missing_inputs.append("acceptance audit event")
        elif len(acceptance) > 1:
            conflicts.append("multiple acceptance audit events")
    if "completion" in required:
        if app.complete and not app.fully_applied:
            conflicts.append("application is marked complete before every entry applied")
        if app.fully_applied and not app.complete:
            missing_inputs.append("final completion record")
    if conflicts:
        classification: EvidenceClassification = "conflict"
    elif missing_digests:
        classification = "unverifiable"
    elif missing_inputs:
        classification = ("recomputed_unbound"
                          if set(missing_inputs) <= {"original candidate binding",
                                                     "accepted review result"}
                          else "unverifiable")
    else:
        classification = "verified"
    provenance = None
    if accepted is not None:
        provenance = ReviewerProvenance.from_review_result(accepted)
        provenance.ownership_valid = ownership_ok
    return EvidenceAssessment(
        operation=operation, application_id=application_id,
        classification=classification, authoritative=classification == "verified",
        missing_inputs=sorted(set(missing_inputs)),
        missing_digests=sorted(set(missing_digests)),
        conflicts=sorted(set(conflicts)), provenance=provenance,
    )


def require_authoritative_evidence(state: RunState, application_id: str, *,
                                   operation: str,
                                   events: list[dict] | None = None) -> EvidenceAssessment:
    """The only authoritative evidence entry point; there is no bypass mode."""
    assessment = classify_review_evidence(
        state, application_id, operation=operation, events=events)
    if assessment.classification == "conflict":
        raise EvidenceConflictError(
            f"Review application {application_id} has conflicting evidence: "
            + "; ".join(assessment.conflicts)
        )
    if assessment.classification == "unverifiable":
        detail = ", ".join(assessment.missing_inputs + assessment.missing_digests)
        raise UnverifiableEvidenceError(
            f"Review application {application_id} cannot be verified for {operation}: {detail}"
        )
    if assessment.classification == "recomputed_unbound":
        raise UnverifiableEvidenceError(
            f"Review application {application_id} is recomputed without an original "
            "binding; authoritative consumption is refused"
        )
    return assessment


def _review_application_events(events: list[dict], application_id: str) -> list[dict]:
    return [event for event in events
            if event.get("kind") == "review_entry_applied"
            and event.get("application_id") == application_id]


class FindingDependency(StrictModel):
    """One reviewer-proposed dependency for a finding or verification stage."""

    dependent_issue_id: str = Field(min_length=1)
    prerequisite_issue_ids: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    independent_findings: list[str] = Field(default_factory=list)
    shared_resource_paths: list[str] = Field(default_factory=list)
    verification_prerequisites: list[str] = Field(default_factory=list)

    @field_validator("shared_resource_paths")
    @classmethod
    def resource_paths_are_safe(cls, values):
        return [safe_relative(value) for value in values]

    @model_validator(mode="after")
    def no_self_dependency(self):
        if self.dependent_issue_id in self.prerequisite_issue_ids:
            raise ValueError("A finding cannot depend on itself")
        if len(set(self.prerequisite_issue_ids)) != len(self.prerequisite_issue_ids):
            raise ValueError("Duplicate prerequisite identity")
        return self


class DependencyRecord(StrictModel):
    """Physical Markdown dependency record associated with approved verdicts."""

    version: Literal[1] = 1
    plan_version: str = Field(min_length=1)
    source_verdicts: list[str] = Field(min_length=1)
    dependencies: list[FindingDependency] = Field(default_factory=list)

    @model_validator(mode="after")
    def dependents_are_unique(self):
        ids = [item.dependent_issue_id for item in self.dependencies]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate dependency record for one issue")
        return self


def validate_dependency_record(record: DependencyRecord, *,
                               known_issue_ids: set[str],
                               issue_owners: dict[str, str],
                               known_worker_ids: set[str],
                               approved_scopes: dict[str, list[str]]) -> None:
    """Validate known IDs, ownership, approved scopes and acyclicity only.

    Scheduling is deliberately out of scope here; the reducer consumes a
    validated record.
    """
    graph: dict[str, list[str]] = {}
    for item in record.dependencies:
        identity = item.dependent_issue_id
        if identity not in known_issue_ids:
            raise ContractError(f"Dependency record names unknown issue {identity}")
        owner = issue_owners.get(identity)
        if owner is None or owner not in known_worker_ids:
            raise ContractError(f"Dependency record issue {identity} has no known owner")
        for prerequisite in item.prerequisite_issue_ids:
            if prerequisite not in known_issue_ids:
                raise ContractError(
                    f"Dependency record {identity} names unknown prerequisite {prerequisite}"
                )
            graph.setdefault(identity, []).append(prerequisite)
        for resource in item.shared_resource_paths:
            owners = [worker for worker, scopes in approved_scopes.items()
                      if any(matches(resource, scope) for scope in scopes)]
            if not owners:
                raise ContractError(
                    f"Dependency record {identity} names shared path {resource} outside "
                    "every approved scope"
                )
        graph.setdefault(identity, [])

    seen: set[str] = set()
    active: set[str] = set()

    def visit(node: str) -> None:
        if node in active:
            raise ContractError(f"Dependency record contains a cycle through {node}")
        if node in seen:
            return
        active.add(node)
        for dependency in graph.get(node, []):
            visit(dependency)
        active.remove(node)
        seen.add(node)

    for node in graph:
        visit(node)


def load_dependency_record(path: Path) -> tuple[DependencyRecord, str]:
    """Parse a reviewed dependency record; validation stays context-specific."""
    value, body = read_markdown(path, DependencyRecord)
    if not isinstance(value, DependencyRecord):
        raise ContractError(f"{path}: expected a dependency record")
    return value, body


def dependency_record_digest(record: DependencyRecord) -> str:
    """Stable digest binding a dependency record to its approved plan version."""
    return _digest(record.model_dump(mode="json"))


def migrate_v1_state(state: RunState, *,
                     issue_lifecycles: dict[str, LifecycleStatus] | None = None,
                     assignment_ids: dict[str, list[str]] | None = None) -> RunState:
    """Explicitly convert version-one state to the canonical schema.

    The caller must supply a lifecycle for every ambiguous legacy open issue and
    every legacy assignment identity that will be used for attribution. Counters,
    evidence, dispositions and lineage are preserved verbatim. Incomplete
    mappings are refused rather than defaulted to fresh allowances.
    """
    if state.version != 1:
        raise ContractError(f"Run state already uses schema version {state.version}")
    mapping = dict(issue_lifecycles or {})
    migrated = state.model_copy(deep=True)
    for identity, issue in migrated.issues.items():
        explicit = mapping.pop(identity, None)
        if issue.status == "open" and issue.has_repair_activity and explicit is None:
            raise ContractError(
                f"Issue {identity}: legacy open status with repair activity is ambiguous; "
                "an explicit pending/in_repair mapping is required"
            )
        target = explicit or issue.current_lifecycle()
        issue.legacy_status = issue.status
        issue.status = target
    if mapping:
        raise ContractError(
            "Lifecycle mapping names unknown issues: " + ", ".join(sorted(mapping))
        )
    assignments = assignment_ids or {}
    for identity, identifiers in assignments.items():
        issue = migrated.issues.get(identity)
        if issue is None:
            raise ContractError(f"Assignment identity mapping names unknown issue {identity}")
        if len(identifiers) != len(issue.assignments):
            raise ContractError(
                f"Issue {identity}: assignment identity inventory is incomplete "
                f"({len(identifiers)} of {len(issue.assignments)} supplied)"
            )
        for assignment, identifier in zip(issue.assignments, identifiers, strict=True):
            if not identifier:
                raise ContractError(f"Issue {identity}: assignment identity must not be empty")
            assignment.assignment_id = identifier
    migrated.version = STATE_SCHEMA_VERSION
    return migrated


def require_operational_schema(state: RunState) -> None:
    """Refuse operational use of version-one state without explicit migration."""
    if state.version != STATE_SCHEMA_VERSION:
        raise ContractError(
            f"Run state schema version {state.version} is historical; operational use "
            "requires an explicit compatible migration"
        )
    for issue in state.issues.values():
        if issue.status not in LIFECYCLE_STATUSES:
            raise ContractError(
                f"Issue {issue.id} retains legacy status {issue.status!r} in canonical state; "
                "explicit migration is required"
            )


Phase = Literal["DRAFT", "DISCUSSING", "PLAN_READY", "APPROVED", "RUNNING", "AWAITING_TEST", "DONE", "ESCALATED", "PAUSED", "ABORTED"]


class RunState(StrictModel):
    # Newly created runs use the canonical schema. Version-one state continues
    # to load and render without a silent rewrite; operational use requires
    # ``migrate_v1_state`` with an explicit inventory.
    version: Literal[1, 2] = STATE_SCHEMA_VERSION
    revision: int = Field(default=0, ge=0)
    run_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    slug: str
    phase: Phase = "DRAFT"
    previous_phase: Phase | None = None
    stage: str = "PREPARE"
    round: int = 1
    approved_hashes: dict[str, str] = Field(default_factory=dict)
    workers: dict[str, dict] = Field(default_factory=dict)
    issues: dict[str, IssueState] = Field(default_factory=dict)
    operations: dict[str, dict] = Field(default_factory=dict)
    # Accepted role results keyed by role, candidate SHA and input-evidence digest.
    review_results: dict[str, ReviewResult] = Field(default_factory=dict)
    # Durable per-review entry application, keyed by application identity.
    review_applications: dict[str, ReviewApplication] = Field(default_factory=dict)
    # Real activity is separate from counters and from issue lifecycle.
    activity: list[ActivityRecord] = Field(default_factory=list)
    # Reviewer-proposed dependency record (physical Markdown, retained verbatim).
    dependency_record: DependencyRecord | None = None
    workspace: dict = Field(default_factory=dict)
    candidate: dict = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    started_at: str | None = None
    updated_at: str | None = None
    error: str | None = None
    pending_control: str | None = None


def approved_files(plan: Path, manifest: Manifest) -> list[Path]:
    names = ["feature.md", "requirement.md", "discussion.md", "plan.md", "manifest.md",
             manifest.reviewers.code, manifest.reviewers.docs]
    names += [w.instruction for w in manifest.workers]
    if manifest.dependency_record:
        names.append(manifest.dependency_record)
    return sorted({within(plan, name) for name in names})


def file_hashes(paths: list[Path]) -> dict[str, str]:
    return {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def effective_settings(feature: FeatureSpec, manifest: Manifest) -> dict[str, str]:
    """Normalized execution settings that approval must pin.

    Covers effective role models and limits after configuration precedence,
    run mode, plan/history flags, and the manifest's configured checks and
    reviewer instructions, so no unhashed configuration file can change how an
    approved run executes. Runtime reports and approval metadata are excluded.
    """
    return {
        "effective_configuration": _digest({
            "feature": feature.model_dump(mode="json"),
            "runtime_settings": feature._runtime_settings,
        }),
        "effective_checks": _digest({
            "checks": [c.model_dump(mode="json") for c in manifest.checks],
            "reviewers": manifest.reviewers.model_dump(mode="json"),
        }),
    }


def approval_hashes(plan: Path, manifest: Manifest, feature: FeatureSpec) -> dict[str, str]:
    """Content hashes of every approved input plus normalized effective settings."""
    try:
        values = file_hashes(approved_files(plan, manifest))
    except OSError as exc:
        raise ContractError(f"Cannot hash approved inputs: {exc}") from exc
    values.update(effective_settings(feature, manifest))
    return values


def detect_approval_changes(
    approved: dict[str, str], plan: Path, manifest: Manifest, feature: FeatureSpec
) -> list[str]:
    """Describe approved inputs that changed, went missing, or appeared.

    Returns an empty list only when every approved input still matches, which
    is the stage-boundary check required before every dispatch. Missing files
    are reported instead of raising so a deleted input still asks for
    re-approval rather than crashing the boundary check.
    """
    actual: dict[str, str] = {}
    missing: set[str] = set()
    for path in approved_files(plan, manifest):
        try:
            actual[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            missing.add(str(path.resolve()))
    actual.update(effective_settings(feature, manifest))
    changes = []
    for key, digest in sorted(approved.items()):
        if key in missing:
            changes.append(f"{key}: missing")
        elif key not in actual:
            changes.append(f"{key}: no longer an approved input")
        elif actual[key] != digest:
            changes.append(f"{key}: content changed")
    for key in sorted(set(actual) - set(approved)):
        changes.append(f"{key}: not covered by the recorded approval")
    return changes
