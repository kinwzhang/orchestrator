"""Strict Markdown contracts, approval hashing and portable path rules for every stage."""
from __future__ import annotations

import hashlib
import json
import re
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


class AssignmentState(StrictModel):
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


class IssueState(StrictModel):
    id: str
    worker: str
    root_issue_id: str
    parent_issue_id: str | None = None
    depth: int = Field(default=0, ge=0)
    status: Literal["open", "resolved", "escalated"] = "open"
    assignments: list[AssignmentState] = Field(
        default_factory=lambda: [AssignmentState(failed_repair_ordinals=[])])
    attempt: int = Field(default=1, ge=1)
    evidence: list[str] = Field(default_factory=list)
    summary: str = ""
    # Reviewer ownership is recorded when a finding is accepted, not inferred
    # from an issue-name prefix. ``system`` covers workflow-created check issues.
    # ``None`` is a legacy record that must be resolved from durable verdict
    # evidence before a reviewer may report or resolve it.
    owner_role: Literal["code", "docs", "system"] | None = None

    @property
    def reassignments(self) -> int:
        """Issue-level reassignment count; maximum one per issue (Q6)."""
        return len(self.assignments) - 1


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


Phase = Literal["DRAFT", "DISCUSSING", "PLAN_READY", "APPROVED", "RUNNING", "AWAITING_TEST", "DONE", "ESCALATED", "PAUSED", "ABORTED"]


class RunState(StrictModel):
    version: Literal[1] = 1
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
