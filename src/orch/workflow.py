"""Deterministic stage scheduling, approval gates and per-issue repair accounting."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from orch.config import Config
from orch.contracts import (
    ApprovalRequiredError,
    AssignmentState,
    CheckError,
    ContractError,
    Discussion,
    FeatureSpec,
    Handover,
    IssueState,
    Manifest,
    RunState,
    Verdict,
    approved_files,
    file_hashes,
    read_markdown,
    within,
)
from orch.reporting import write_report
from orch.store import StateStore, atomic_write, timestamp


class ApprovalRequired(ApprovalRequiredError):
    pass


class Escalation(RuntimeError):
    pass


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


def approval_hashes(plan: Path, manifest: Manifest, feature: FeatureSpec) -> dict[str, str]:
    values = file_hashes(approved_files(plan, manifest))
    values["effective_configuration"] = hashlib.sha256(
        feature.model_dump_json().encode("utf-8")
    ).hexdigest()
    return values


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


def route_verdicts(state: RunState, verdicts: list[Verdict], manifest: Manifest) -> set[str]:
    """Update issue lineage/counters; code quality is entirely the reviewers' decision."""
    workers = {w.id for w in manifest.workers}
    findings = {}
    for verdict in verdicts:
        if verdict.result == "escalate":
            raise Escalation(f"{verdict.reviewer} reviewer requested escalation")
        for finding in verdict.findings:
            if finding.worker not in workers or not set(finding.related_workers) <= workers:
                raise ContractError(f"Unknown finding owner: {finding.worker}")
            if finding.id in findings and findings[finding.id] != finding:
                raise ContractError(f"Reviewers disagree on identity of {finding.id}")
            findings[finding.id] = finding
    remaining = dict(findings)
    while remaining:
        progress = False
        for identity, finding in list(remaining.items()):
            previous = state.issues.get(identity)
            if finding.parent_issue_id and finding.parent_issue_id not in state.issues:
                continue
            parent = state.issues.get(finding.parent_issue_id)
            depth = parent.depth + 1 if parent else 0
            root = parent.root_issue_id if parent else identity
            if finding.root_issue_id != root:
                raise ContractError(f"Incorrect root for {identity}")
            if previous:
                if (previous.parent_issue_id, previous.worker) != (finding.parent_issue_id, finding.worker):
                    raise ContractError(f"Issue identity changed: {identity}")
                current = previous.assignments[-1]
                current.review_rounds += 1
                if current.repairs:
                    current.failed_repairs += 1
                previous.status = "open"
            else:
                previous = IssueState(id=identity, worker=finding.worker, root_issue_id=root,
                                      parent_issue_id=finding.parent_issue_id, depth=depth,
                                      summary=finding.summary)
                state.issues[identity] = previous
            if depth > manifest.limits.max_issue_depth:
                state.issues[root].status = "escalated"
                raise Escalation(f"Derived issue {identity} exceeds depth limit; escalate root {root}")
            if finding.action == "escalate":
                previous.status = "escalated"
                raise Escalation(f"Issue {identity} requires escalation")
            current = previous.assignments[-1]
            exhausted = (current.failed_repairs >= manifest.limits.max_failed_repairs_per_assignment
                         or current.repairs >= manifest.limits.max_repair_attempts
                         or current.review_rounds >= manifest.limits.max_review_rounds)
            if exhausted or finding.action == "reassign":
                if len(previous.assignments) - 1 >= manifest.limits.max_reassignments:
                    previous.status = "escalated"
                    raise Escalation(f"Issue {identity} exhausted replacement allowance")
                previous.assignments.append(AssignmentState())
            del remaining[identity]
            progress = True
        if not progress:
            raise ContractError("Issue lineage has missing parents or a cycle")
    resolved = {identity for v in verdicts for identity in v.resolved}
    if resolved & findings.keys():
        raise ContractError("Issue is simultaneously resolved and open")
    for identity in resolved:
        if identity not in state.issues:
            raise ContractError(f"Unknown resolved issue: {identity}")
        state.issues[identity].status = "resolved"
    if all(v.result == "pass" for v in verdicts):
        for issue in state.issues.values():
            if issue.status == "open" and issue.id not in resolved:
                raise ContractError(f"Passing verdict must explicitly resolve {issue.id}")
    return {f.worker for f in findings.values()}


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
        from orch.agents import AgentRequest
        info = self.state.workers.setdefault(logical_id, {})
        attempt = int(info.get("attempt", 0)) + 1
        attempt_id = f"{logical_id}-{self.state.round}-{attempt}"
        log_dir = self.run_dir / f"round-{self.state.round}" / "logs" / attempt_id
        model = self.feature.models.resolve(role)
        info.update(status="running", role=role, attempt=attempt, activity=timestamp(),
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
            if kind in {"process_started", "process_children", "process_completed"}:
                self.event(kind, agent=logical_id, attempt=attempt_id,
                           **{k: v for k, v in event.items() if k != "kind"})
            self.save()
        try:
            result = await adapter.run(request, on_event=activity)
        except asyncio.CancelledError:
            info["status"] = "interrupted"
            self.save()
            self.event("agent_interrupted", agent=logical_id, attempt=attempt_id)
            raise
        except Exception as exc:
            info.update(status="failed", activity=timestamp())
            self.save()
            self.event("agent_failed", agent=logical_id, attempt=attempt_id,
                       error=_failure_message(exc))
            raise
        info.update(status="done" if result.exit_code == 0 else "failed", activity=timestamp(),
                    duration_seconds=result.duration_seconds)
        self.save()
        self.event("agent_completed", agent=logical_id, attempt=attempt_id,
                   exit_code=result.exit_code, duration_seconds=result.duration_seconds,
                   usage=result.usage, model=model.model, adapter_version=result.adapter_version)
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
        self.check_approval()
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

    async def run_worker(self, worker):
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
            if info.get("handover"):
                inputs.append(Path(info["handover"]))
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
                                                      summary=str(exc))
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
            import re
            text = re.sub(r"(?ms)^\s*```.*?^\s*```[^\n]*\n?", "[Code block omitted]\n", text)
            atomic_write(destination / f"{index:03d}-{source.name}", text)
        return destination

    async def reviewer(self, role, candidate):
        logical = "review-code" if role == "code" else "review-docs"
        output = self.run_dir / f"round-{self.state.round}" / f"verdict-{role}.md"
        if role == "docs":
            cwd = self.docs_inputs()
            inputs = sorted(cwd.glob("*.md"))
            extra = "Measure relevance of subagent documentation and handovers only. Read no code, source diffs, source checkout or logs. Use only supplied Markdown files."
        else:
            cwd = Path(candidate.path)
            inputs = [self.plan / self.manifest.reviewers.code, self.plan / "requirement.md", self.plan / "plan.md", self.run_dir / "issues.md"]
            inputs += [Path(self.state.workers[w.id]["handover"]) for w in self.manifest.workers]
            extra = "Review integration implementation against requirements and acceptance criteria."
        extra += f"\nCandidate SHA: {candidate.sha}. Write strict {role} verdict to {output}. Preserve existing issue IDs; identify new issue parent/root IDs and explicitly list resolved IDs."
        for attempt in range(2):
            instruction = self.input_file(f"{logical}-{attempt}", "Review candidate", inputs, extra)
            await self.dispatch("code_reviewer" if role == "code" else "docs_reviewer", logical,
                                instruction, output, cwd, inputs)
            try:
                verdict, _ = read_markdown(output, Verdict)
                if verdict.reviewer != role:
                    raise ContractError("Wrong reviewer identity")
                return verdict
            except (ContractError, OSError) as exc:
                if attempt:
                    raise Escalation(f"Malformed {role} verdict after one repair: {exc}") from exc
                self.event("verdict_repair", agent=logical, error=str(exc))
                extra += f"\nYour previous verdict was invalid: {exc}. Repair it once."
        raise AssertionError("unreachable")

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
                        issue = IssueState(id=key, worker=worker.id, root_issue_id=key, summary=str(exc))
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
            candidate = Candidate(**{**self.state.candidate, "path": Path(self.state.candidate["path"])})
            if self.manifest.limits.concurrency > 1:
                async with asyncio.TaskGroup() as group:
                    code = group.create_task(self.reviewer("code", candidate))
                    docs = group.create_task(self.reviewer("docs", candidate))
                verdicts = [code.result(), docs.result()]
            else:
                verdicts = [await self.reviewer("code", candidate),
                            await self.reviewer("docs", candidate)]
            from orch.git_ops import git, head
            if head(candidate.path) != candidate.sha or git(candidate.path, "status", "--porcelain").strip():
                raise Escalation("Reviewer changed the candidate; verdicts invalidated")
            affected = route_verdicts(self.state, verdicts, self.manifest)
            if affected:
                # Invalidate transitive dependents; they must see updated dependency code.
                while True:
                    expanded = affected | {w.id for w in self.manifest.workers if set(w.depends_on) & affected}
                    if expanded == affected:
                        break
                    affected = expanded
                for worker in affected:
                    info = self.state.workers[worker]
                    info["checked"] = False
                    info["status"] = "rework"
                    feedback = self.run_dir / f"round-{self.state.round}" / "feedback" / f"{worker}.md"
                    atomic_write(feedback, "# Review feedback\n\n" + "\n".join(
                        f"Read {self.run_dir / f'round-{self.state.round}' / f'verdict-{v.reviewer}.md'}" for v in verdicts))
                    info["feedback"] = str(feedback)
                self.event("rework_issued", agents=sorted(affected))
                self.state.round += 1
                self.transition(stage="WORK")
            else:
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
            self.git.commit_plan(self.plan, message=f"orch: {phase.lower()} report")
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

    async def reconcile_processes(self):
        """Cancel positively identified survivors before restarting interrupted calls."""
        import psutil

        from orch.processes import ProcessHandle, reconcile_events
        for identity in reconcile_events(self.store.events(self.state.run_id)):
            for live in identity["live"]:
                await self.runner.cancel(ProcessHandle(**live))
            self.event("process_completed", pid=identity["pid"], create_time=identity["create_time"],
                       reason="reconciled-after-interruption")
        for name, info in self.state.workers.items():
            if info.get("status") != "running":
                continue
            process = info.get("process")
            if process and "create_time" in process:
                try:
                    observed = psutil.Process(process["pid"])
                    if abs(observed.create_time() - process["create_time"]) < 0.01:
                        await self.runner.cancel(ProcessHandle(**process))
                except psutil.NoSuchProcess:
                    pass
            elif process:
                raise Escalation(f"Cannot establish ownership of interrupted process for {name}")
            info["status"] = "interrupted"
            self.event("interrupted_job_reconciled", agent=name)
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
