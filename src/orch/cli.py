"""Command-line lifecycle controls. All feature-specific inputs live in Markdown."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import portalocker

from orch.config import ConfigResolution, effective_feature, resolve_config
from orch.contracts import (
    ContractError,
    Discussion,
    FeatureSpec,
    Handover,
    Manifest,
    RunState,
    Verdict,
    read_markdown,
    write_markdown,
)
from orch.store import StateStore, atomic_write, timestamp
from orch.workflow import (
    Engine,
    approval_hashes,
    next_action,
    recover_review_batch,
    validate_plan,
)


def parser():
    root = argparse.ArgumentParser(prog="orch", description="Markdown-driven local delivery",
                                   allow_abbrev=False)
    root.add_argument("--data-dir", type=Path, help="Override platform state directory")
    root.add_argument(
        "--global-models", action="store_true",
        help="Use global model preferences instead of repository role overrides")
    commands = root.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", help="Create a feature checkout and Markdown settings")
    new.add_argument("slug")
    new.add_argument("--target", type=Path, default=Path.cwd())
    new.add_argument("--requirement", type=Path)
    new.add_argument("--base", default="main")
    for name in ("plan", "approve", "run", "next", "pause", "resume", "done", "abort", "clean"):
        command = commands.add_parser(name)
        command.add_argument("slug")
    watch = commands.add_parser("watch")
    watch.add_argument("slug", nargs="?")
    watch.add_argument("--once", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("slug", nargs="?")
    reopen = commands.add_parser("reopen")
    reopen.add_argument("slug")
    reopen.add_argument("notes_file", type=Path)
    recover = commands.add_parser(
        "recover-review",
        help="Audited recovery of a partially applied legacy cross-role review batch",
    )
    recover.add_argument("slug")
    recover.add_argument("--operator", required=True)
    recover.add_argument("--reason", required=True)
    recover.add_argument("--dry-run", action="store_true")
    validate = commands.add_parser("validate", help="Validate a Markdown contract")
    validate.add_argument("kind", choices=["feature", "manifest", "discussion", "handover", "verdict"])
    validate.add_argument("file", type=Path)
    models = commands.add_parser("models", allow_abbrev=False,
                                 help="Inspect or change model preferences")
    model_commands = models.add_subparsers(dest="models_command", required=True)
    show = model_commands.add_parser("show", allow_abbrev=False)
    selectors = show.add_mutually_exclusive_group()
    selectors.add_argument("--target", type=Path)
    selectors.add_argument("--feature")
    change = model_commands.add_parser("set", allow_abbrev=False)
    change.add_argument("role")
    change.add_argument("--model")
    change.add_argument("--reasoning")
    change.add_argument("--scope", choices=["global", "repo"], default="global")
    change.add_argument("--target", type=Path)
    return root


def registry_path(store, slug):
    # Reuse the store's validated run ID rule for slugs supplied by CLI.
    store.run_dir(slug)
    return store.data_dir / "features" / f"{slug}.json"


def registry(store):
    directory = store.data_dir / "features"
    return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))]


def locate(store, slug=None):
    entries = registry(store)
    if slug:
        matches = [entry for entry in entries if entry["slug"] == slug or entry["run_id"] == slug]
    else:
        matches = entries[-1:]
    if len(matches) != 1:
        raise ContractError(f"Unknown or ambiguous feature: {slug}")
    return matches[0]


def context(store, slug, global_models=False):
    from orch.models import consent_resolution
    entry = locate(store, slug)
    source = Path(entry["plan"]) / "feature.md"
    feature, _ = read_markdown(source, FeatureSpec)
    resolution = consent_resolution(
        Path(entry["repo"]), global_models=global_models, feature_path=source)
    if global_models:
        # Announce the invocation-local selection before any lifecycle action
        # or dispatch can use it. Inspection paths print the same notice in
        # their preference view.
        print("Model selection: global preferences (--global-models); "
              "repository model overrides are ignored for this command.")
    feature = effective_feature(feature, resolution.config, source)
    state = store.load(entry["run_id"])
    return entry, feature, state, resolution


def new_feature(store, args):
    from orch.git_ops import GitWorkspace
    path = registry_path(store, args.slug)
    existing = json.loads(path.read_text()) if path.exists() else None
    if existing and existing.get("status") != "creating":
        raise ContractError(f"Feature already exists: {args.slug}")
    target = args.target.resolve()
    requirement = args.requirement.resolve() if args.requirement else target / "requirement.md"
    feature = FeatureSpec(slug=args.slug, target_project=target, requirement=requirement,
                          base_branch=args.base)
    run_id = existing["run_id"] if existing else f"{args.slug}-{uuid.uuid4().hex[:8]}"
    plan = store.run_dir(run_id) / "worktrees" / "feature" / ".plans" / args.slug
    entry = {"slug": args.slug, "run_id": run_id, "repo": str(target), "plan": str(plan),
             "status": "creating"}
    if existing and (existing["repo"] != str(target) or existing["plan"] != str(plan)):
        raise ContractError("Interrupted creation must resume with the original target")
    atomic_write(path, json.dumps(entry, indent=2))
    workspace = GitWorkspace(target, store.run_dir(run_id)).prepare(feature)
    plan = workspace.feature_path / ".plans" / args.slug
    plan.mkdir(parents=True, exist_ok=True)
    if not (plan / "feature.md").exists():
        write_markdown(plan / "feature.md", feature, "# Feature settings\n\nEdit these settings before planning.\n")
    if (plan / "requirement.md").exists():
        pass
    elif requirement.exists():
        shutil.copyfile(requirement, plan / "requirement.md")
    else:
        atomic_write(plan / "requirement.md", "# Requirement\n\nDescribe the feature and acceptance criteria.\n")
    if not (plan / "discussion.md").exists():
        write_markdown(plan / "discussion.md", Discussion(), "# Discussion\n\nRecord decisions as they are made.\n")
    state = store.load(run_id) if (store.run_dir(run_id) / "state.json").exists() else RunState(run_id=run_id, slug=args.slug, workspace=workspace.to_dict())
    state = store.save(state)
    entry["status"] = "ready"
    atomic_write(path, json.dumps(entry, indent=2))
    store.event(run_id, "feature_created", branch=workspace.feature_branch, plan=str(plan))
    print(plan / "feature.md")
    return state


_PLANNER_TERMINAL_EVENTS = {
    "process_completed",
    "planning_exited",
    "planning_interrupted",
    "planning_launch_failed",
}


def _planner_attempt_has_terminal_evidence(events, attempt, after_id):
    """Only terminal evidence recorded after the specific dispatch intent counts.

    A completion from an earlier launch must never resolve a later dispatch,
    even when legacy or corrupted state repeats an attempt ID.
    """
    related = [event for event in events
               if event.get("agent") == "planner" and event.get("attempt") == attempt
               and event.get("id", 0) > after_id]
    if any(event.get("kind") == "planning_launch_failed" for event in related):
        return True
    starts = set()
    for event in sorted(related, key=lambda item: item.get("id", 0)):
        pid, create_time = event.get("pid"), event.get("create_time")
        identity = (pid, create_time)
        if (event.get("kind") == "process_started" and isinstance(pid, int)
                and isinstance(create_time, (int, float)) and create_time > 0):
            starts.add(identity)
        elif (event.get("kind") == "process_completed"
              and isinstance(pid, int)
              and isinstance(create_time, (int, float))
              and create_time > 0
              and identity in starts):
            return True
    return False


def _planner_next_attempt(events, planner):
    """Return a persisted counter that never reuses an attempt seen on disk."""
    stored = planner.get("attempt")
    counter = stored if isinstance(stored, int) and not isinstance(stored, bool) and stored > 0 else 0
    observed = 0
    for event in events:
        if event.get("agent") != "planner":
            continue
        attempt_id = event.get("attempt")
        if not isinstance(attempt_id, str):
            continue
        match = re.fullmatch(r"planner-\d+-(\d+)", attempt_id)
        if match:
            observed = max(observed, int(match.group(1)))
    return max(counter, observed) + 1


def _planner_recovery_escalation(store, state, attempts, reason):
    names = ", ".join(attempts) if attempts else "unidentified legacy attempt"
    message = f"Planner dispatch {names} is unresolved ({reason}); refusing to launch Codex"
    if state.phase != "ESCALATED":
        state.previous_phase = state.phase
    state.phase = "ESCALATED"
    state.error = message
    store.save(state)
    store.event(state.run_id, "planner_recovery_escalated", attempts=attempts, reason=reason)
    raise ContractError(message)


def reconcile_planner_dispatches(store, state):
    """Only terminal, attempt-correlated evidence permits another planner launch.

    This intentionally never terminates an unresolved planner. A missing or
    stale PID is not proof that its process tree ended, and process identity by
    itself is not completion evidence.
    """
    import psutil

    events = store.events(state.run_id)
    planner_events = [event for event in events if event.get("agent") == "planner"]
    intents = [event for event in planner_events if event.get("kind") == "dispatch_intent"]
    attempts_by_key = {}
    ambiguous = {}
    legacy_pending = False
    unresolved = []

    for intent in intents:
        attempt = intent.get("attempt")
        if isinstance(attempt, str) and attempt:
            attempts_by_key.setdefault(attempt, []).append(intent)
            continue

        # Pre-repair planner events lack attempt IDs. Pair them in journal order
        # only; if their boundaries overlap or no terminal record follows, fail
        # closed rather than borrowing another attempt's PID or completion.
        if legacy_pending:
            unresolved.append(f"legacy-intent-{intent.get('id')}")
        legacy_pending = True
        for event in planner_events:
            if event.get("id", 0) <= intent.get("id", 0):
                continue
            if event.get("attempt") is None and event.get("kind") in _PLANNER_TERMINAL_EVENTS:
                legacy_pending = False
                break
            if event.get("kind") == "dispatch_intent" and event.get("agent") == "planner":
                break
        if legacy_pending:
            unresolved.append(f"legacy-intent-{intent.get('id')}")

    for attempt, attempt_intents in attempts_by_key.items():
        if len(attempt_intents) > 1:
            # Repeated dispatch IDs cannot identify one owner. Legacy or
            # corrupted journals fail closed instead of borrowing completion
            # evidence across occurrences.
            ambiguous[attempt] = "dispatch ID repeats in persisted evidence"
            unresolved.append(attempt)
            continue
        intent = attempt_intents[0]
        if not _planner_attempt_has_terminal_evidence(
                planner_events, attempt, intent.get("id", 0)):
            unresolved.append(attempt)

    # A start event without an intent cannot establish which planner attempt
    # owns it. It must never be adopted as authority for a new invocation.
    for event in planner_events:
        if event.get("kind") != "process_started":
            continue
        attempt = event.get("attempt")
        if isinstance(attempt, str):
            if attempt not in attempts_by_key:
                unresolved.append(attempt)
        else:
            legacy_intents = [
                intent for intent in intents
                if not isinstance(intent.get("attempt"), str)
                and intent.get("id", 0) < event.get("id", 0)
            ]
            legacy_intent = max(legacy_intents, key=lambda item: item.get("id", 0), default=None)
            if legacy_intent is None or any(
                prior.get("kind") in _PLANNER_TERMINAL_EVENTS
                and prior.get("attempt") is None
                and legacy_intent.get("id", 0) < prior.get("id", 0) < event.get("id", 0)
                for prior in planner_events
            ):
                unresolved.append("unmatched-process-start")

    planner = state.workers.get("planner", {})
    current_attempt = planner.get("uncertain_attempt_id") or planner.get("attempt_id")
    current_identity = None
    if (current_attempt and planner.get("process_attempt_id") == current_attempt
            and isinstance(planner.get("process"), dict)):
        current_identity = {**planner["process"], "attempt": current_attempt}

    if (planner.get("status") in {"running", "starting", "uncertain"} and current_attempt
            and current_attempt not in attempts_by_key):
        # State is written before the dispatch-intent event. Therefore an
        # attempt with no intent is known not to have reached Popen unless a
        # process identity was already persisted, which is inconsistent.
        if (planner.get("process_attempt_id") == current_attempt
                or planner.get("uncertain_attempt_id") == current_attempt):
            _planner_recovery_escalation(
                store, state, [current_attempt],
                "attempt-specific process evidence exists without its dispatch intent",
            )
        planner.update(status="interrupted", process=None, process_attempt_id=None)
        state = store.save(state)
        store.event(state.run_id, "planning_launch_not_started", attempt=current_attempt)

    if unresolved:
        unresolved = sorted(set(unresolved))
        evidence = []
        for attempt in unresolved:
            if attempt in ambiguous:
                evidence.append(f"{attempt}: {ambiguous[attempt]}; cannot choose an owner")
                continue
            attempt_intents = attempts_by_key.get(attempt, [])
            after_id = attempt_intents[0].get("id", 0) if attempt_intents else 0
            # Only evidence from this dispatch occurrence may describe it.
            related = [event for event in planner_events
                       if event.get("attempt") == attempt and event.get("id", 0) > after_id]
            identities = [
                {"pid": event.get("pid"), "create_time": event.get("create_time"),
                 "source": "process_started"}
                for event in related if event.get("kind") == "process_started"
            ]
            if current_identity and current_identity.get("attempt") == attempt:
                identities.append({**current_identity, "source": "saved_state"})
            if not identities:
                evidence.append(f"{attempt}: no matching durable process identity")
                continue
            for identity in identities:
                pid, create_time = identity.get("pid"), identity.get("create_time")
                if not isinstance(pid, int) or not isinstance(create_time, (int, float)) or create_time <= 0:
                    evidence.append(f"{attempt}: invalid identity evidence")
                    continue
                try:
                    observed = psutil.Process(pid)
                    if abs(observed.create_time() - create_time) > 0.01:
                        evidence.append(f"{attempt}: process identity no longer matches")
                    elif observed.status() == psutil.STATUS_ZOMBIE:
                        evidence.append(f"{attempt}: process exited without terminal evidence")
                    else:
                        evidence.append(f"{attempt}: matching process may still be active")
                except psutil.NoSuchProcess:
                    evidence.append(f"{attempt}: process is absent but terminal evidence is missing")
                except psutil.Error:
                    evidence.append(f"{attempt}: process identity cannot be verified")
        _planner_recovery_escalation(store, state, unresolved, "; ".join(evidence))

    return state


def plan_feature(store, entry, feature, state):
    import psutil

    from orch.agents import AgentRequest, CodexAdapter
    from orch.platform_support import executable_argv
    # Reconcile before writing a new instruction or changing planner state. This
    # path is shared by both `orch plan` and `orch resume` from DISCUSSING.
    state = reconcile_planner_dispatches(store, state)
    plan = Path(entry["plan"])
    planner = state.workers.setdefault("planner", {})
    attempt = _planner_next_attempt(store.events(state.run_id), planner)
    attempt_id = f"planner-{state.round}-{attempt}"
    instruction = store.run_dir(state.run_id) / f"planning-instruction-{attempt_id}.md"
    model = feature.models.resolve("planner")
    template = Path(__file__).parent / "templates" / "planner.md"
    body = template.read_text() if template.exists() else "Maintain discussion.md and write all plan artifacts."
    atomic_write(instruction, f"# Planning session\n\nCanonical plan folder: {plan}\nRequirement: {plan / 'requirement.md'}\nDiscussion: {plan / 'discussion.md'}\n\n{body}\n")
    state.phase = "DISCUSSING"
    state = store.save(state)
    request = AgentRequest(role="planner", logical_id="planner", attempt_id=attempt_id,
                           instruction_file=instruction, output_file=plan / "plan.md",
                           cwd=Path(state.workspace["feature_path"]), model=model,
                           writable_roots=(plan,))
    argv = CodexAdapter().interactive_argv(request)
    planner = state.workers.setdefault("planner", {})
    planner.update(status="running", role="planner", attempt=attempt, attempt_id=attempt_id,
                   started_at=timestamp(), output=str(request.output_file))
    planner.pop("process", None)
    planner.pop("process_attempt_id", None)
    state = store.save(state)
    store.event(state.run_id, "dispatch_intent", agent="planner", attempt=attempt_id,
                role="planner", model=model.model, instruction=str(instruction),
                output=str(request.output_file))

    def stop_owned_process(process, identity):
        """Stop only the planner process tree whose creation identity was recorded."""
        parent = psutil.Process(identity["pid"])
        if abs(parent.create_time() - identity["create_time"]) > 0.01:
            raise ContractError("Planner process identity changed; refusing to terminate it")
        processes = [*parent.children(recursive=True), parent]
        for child in reversed(processes):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(processes, timeout=1.0)
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        process.wait()

    info = state.workers.setdefault("planner", {})
    info.update(status="running", role="planner", attempt=attempt, attempt_id=attempt_id,
                started_at=timestamp())
    state = store.save(state)
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    try:
        process = subprocess.Popen(executable_argv(argv), cwd=request.cwd, **kwargs)
    except OSError:
        info = state.workers["planner"]
        info["status"] = "failed"
        store.save(state)
        store.event(state.run_id, "planning_launch_failed", agent="planner", attempt=attempt_id,
                    error="Popen failed before creating a process")
        raise
    try:
        created = psutil.Process(process.pid).create_time()
    except psutil.Error as exc:
        info = state.workers["planner"]
        info.update(status="uncertain", uncertain_attempt_id=attempt_id,
                    process={"pid": process.pid, "create_time": None},
                    process_attempt_id=attempt_id)
        state.phase = "ESCALATED"
        state.previous_phase = "DISCUSSING"
        state.error = f"Planner {attempt_id} started but its process identity cannot be verified"
        state = store.save(state)
        store.event(state.run_id, "planning_identity_uncertain", agent="planner",
                    attempt=attempt_id, pid=process.pid, error=str(exc))
        raise ContractError(state.error) from exc
    if created <= 0:
        info = state.workers["planner"]
        info.update(status="uncertain", uncertain_attempt_id=attempt_id,
                    process={"pid": process.pid, "create_time": created},
                    process_attempt_id=attempt_id)
        state.phase = "ESCALATED"
        state.previous_phase = "DISCUSSING"
        state.error = f"Planner {attempt_id} started without a verifiable process identity"
        state = store.save(state)
        store.event(state.run_id, "planning_identity_uncertain", agent="planner",
                    attempt=attempt_id, pid=process.pid, create_time=created)
        raise ContractError(state.error)
    identity = {"pid": process.pid, "create_time": created}
    info = state.workers["planner"]
    info["process"] = identity
    info["process_attempt_id"] = attempt_id
    state = store.save(state)
    store.event(state.run_id, "process_started", agent="planner", attempt=attempt_id, **identity)

    interrupted = None
    try:
        while process.poll() is None:
            control_path = store.run_dir(state.run_id) / "control.json"
            if control_path.exists():
                try:
                    request_kind = json.loads(control_path.read_text(encoding="utf-8"))["request"]
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise ContractError(f"Invalid planner control request: {exc}") from exc
                if request_kind not in {"pause", "abort"}:
                    raise ContractError(f"Unsupported planner control request: {request_kind}")
                control_path.unlink(missing_ok=True)
                interrupted = request_kind
                stop_owned_process(process, identity)
                break
            time.sleep(0.1)
        if interrupted is None:
            # Catch a request that arrived as the process exited.
            control_path = store.run_dir(state.run_id) / "control.json"
            if control_path.exists():
                request_kind = json.loads(control_path.read_text(encoding="utf-8"))["request"]
                if request_kind in {"pause", "abort"}:
                    control_path.unlink(missing_ok=True)
                    interrupted = request_kind
    except KeyboardInterrupt:
        interrupted = "pause"
        stop_owned_process(process, identity)

    if interrupted:
        state.previous_phase = state.phase
        state.phase = "ABORTED" if interrupted == "abort" else "PAUSED"
        state.workers["planner"].update(status="interrupted", process=identity)
        state = store.save(state)
        store.event(state.run_id, "process_completed", agent="planner", attempt=attempt_id,
                    exit_code=process.returncode, reason=interrupted, **identity)
        store.event(state.run_id, "planning_interrupted", agent="planner", attempt=attempt_id,
                    request=interrupted,
                    exit_code=process.returncode, **identity)
        return state

    store.event(state.run_id, "process_completed", agent="planner", attempt=attempt_id,
                exit_code=process.returncode, **identity)
    state.workers["planner"].update(status="done" if process.returncode == 0 else "failed",
                                    exit_code=process.returncode)
    state = store.save(state)
    store.event(state.run_id, "planning_exited", agent="planner", attempt=attempt_id,
                exit_code=process.returncode)
    if process.returncode:
        raise ContractError("Planning session exited unsuccessfully; discussion files retained")
    try:
        validate_plan(plan)
    except (ContractError, OSError):
        print("Discussion saved; required plan artifacts/questions are not yet complete.")
        return state
    state.phase = "PLAN_READY"
    state = store.save(state)
    print("Plan ready. Review files, then run orch approve.")
    return state


def approve_feature(store, entry, feature, state):
    from orch.git_ops import GitWorkspace, WorkspaceRecord, head
    if state.phase not in {"DRAFT", "PLAN_READY", "DISCUSSING", "APPROVED", "ESCALATED", "PAUSED"}:
        raise ContractError(f"Cannot approve from {state.phase}")
    plan = Path(entry["plan"])
    manifest = validate_plan(plan)
    record = WorkspaceRecord.from_dict(state.workspace)
    if manifest.slug != feature.slug or manifest.branch != record.feature_branch or manifest.base_branch != feature.base_branch:
        raise ContractError("Manifest feature/branch/base does not match canonical checkout")
    # Explicitly bind per-feature limits to the approved manifest contract.
    feature.limits = manifest.limits
    state.approved_hashes = approval_hashes(plan, manifest, feature)
    if feature.commit_plan:
        GitWorkspace(feature.target_project, store.run_dir(state.run_id)).commit_plan(plan)
    state.workspace["feature_head"] = head(record.feature_path)
    state.error = None
    state.phase = "APPROVED"
    state = store.save(state)
    store.event(state.run_id, "plan_approved", hashes=state.approved_hashes)
    print(f"Approved {feature.slug}; next stage: {state.stage}")
    return state


def engine(store, entry, feature, state, resolution: ConfigResolution | None = None):
    manifest, _ = read_markdown(Path(entry["plan"]) / "manifest.md", Manifest)
    feature.limits = manifest.limits
    from orch.processes import ProcessRunner
    repo = Path(entry["repo"])
    source = Path(entry["plan"]) / "feature.md"
    if resolution is None:
        from orch.models import consent_resolution
        resolution = consent_resolution(repo, feature_path=source)
    config = resolution.config
    consented = (tuple(resolution.findings),
                 json.dumps(resolution.config.models.model_dump(mode="json"), sort_keys=True))

    def settings_loader():
        fresh = resolve_config(repo, global_models=resolution.global_models)
        if fresh.findings:
            current = (tuple(fresh.findings),
                       json.dumps(fresh.config.models.model_dump(mode="json"), sort_keys=True))
            if current != consented:
                from orch.workflow import ApprovalRequired
                raise ApprovalRequired(
                    "Configuration now requires a fresh fallback decision; run orch again")
        refreshed, _ = read_markdown(source, FeatureSpec)
        return effective_feature(refreshed, fresh.config, source), fresh.config

    runner = ProcessRunner([os.environ.get(name, "") for name in config.redaction_env])
    return Engine(store, state, feature, manifest, Path(entry["plan"]), runner=runner,
                  config=config, settings_loader=settings_loader)


def control(store, state, request):
    atomic_write(store.run_dir(state.run_id) / "control.json", json.dumps({"request": request}))
    store.event(state.run_id, "control_requested", request=request)
    try:
        with store.lock(f"feature-{state.slug}"):
            # No live runner holds this feature lock. Apply the request now, including
            # recovery from a process that died while state still says RUNNING.
            state = store.load(state.run_id)
            state.previous_phase = state.phase
            state.phase = "ABORTED" if request == "abort" else "PAUSED"
            state = store.save(state)
            (store.run_dir(state.run_id) / "control.json").unlink(missing_ok=True)
    except portalocker.exceptions.LockException:
        pass  # The live runner observes the durably queued request.
    return state


# Only commands that resolve effective model preferences accept the root flag.
# `done` and `clean` are lifecycle bookkeeping: they neither resolve nor use
# model selection, so accepting the flag would misrepresent the invocation.
# `recover-review` reuses the same context resolution as the lifecycle commands.
PREFERENCE_COMMANDS = frozenset(
    {"next", "plan", "approve", "run", "resume", "reopen", "recover-review"})


def run_command(args):
    global_models = bool(getattr(args, "global_models", False))
    if args.command == "models":
        from orch.models import run_models
        if global_models and getattr(args, "models_command", None) == "set":
            raise ContractError("--global-models cannot be combined with models set")
        return run_models(args)
    if global_models and args.command not in PREFERENCE_COMMANDS:
        raise ContractError(
            f"--global-models does not apply to the {args.command} command")
    if args.command == "validate":
        schema = {"feature": FeatureSpec, "manifest": Manifest, "discussion": Discussion,
                  "handover": Handover, "verdict": Verdict}[args.kind]
        read_markdown(args.file, schema)
        print(f"Valid {args.kind}: {args.file}")
        return 0
    config = resolve_config(Path.cwd(), global_models=global_models).config
    store = StateStore(args.data_dir or config.data_dir)
    if args.command == "new":
        with store.lock(f"feature-{args.slug}"):
            new_feature(store, args)
        return 0
    if args.command == "status":
        entries = [locate(store, args.slug)] if args.slug else registry(store)
        for entry in entries:
            if entry.get("status") == "creating":
                print(f"{entry['slug']}: creation interrupted; repeat orch new with the original options")
                continue
            state = store.load(entry["run_id"])
            print(f"{state.slug}: {state.phase} / {state.stage} / cycle {state.round}; next: {next_action(state)}")
            print(f"  Plan: {entry['plan']}\n  Run: {store.run_dir(state.run_id)}")
            if args.slug:
                for agent, info in state.workers.items():
                    print(f"  {agent}: {info.get('status', 'pending')}; attempt {info.get('attempt', 0)}; checks {info.get('checks', 'pending')}")
                for issue in state.issues.values():
                    legacy = f" (legacy {issue.legacy_status})" if issue.legacy_status else ""
                    print(f"  Issue {issue.id}: {issue.lifecycle}{legacy}; "
                          f"root {issue.root_issue_id}; {issue.summary}")
                for application in state.review_applications.values():
                    print(f"  Application {application.application_id}: "
                          f"{len(application.applied_entry_ids)}/{len(application.entries)} "
                          f"entries applied; next {application.next_entry_index}; "
                          f"{'complete' if application.complete else 'incomplete'}")
                if state.dependency_record is not None:
                    print(f"  Dependency record: plan {state.dependency_record.plan_version}; "
                          f"{len(state.dependency_record.dependencies)} dependent issue(s)")
                if state.error:
                    print(f"  Blocker: {state.error}")
        return 0
    entry = locate(store, args.slug)
    if args.command == "watch":
        from orch.watch import watch
        watch(store, entry["run_id"], once=args.once)
        return 0
    if args.command in {"pause", "abort"}:
        # Control commands stay free of model resolution and never prompt.
        control(store, store.load(entry["run_id"]), args.command)
        return 0
    entry, feature, state, resolution = context(store, args.slug, global_models=global_models)
    if args.command == "recover-review":
        with store.lock(f"feature-{state.slug}"):
            manifest, _ = read_markdown(Path(entry["plan"]) / "manifest.md", Manifest)
            feature.limits = manifest.limits
            summary = recover_review_batch(
                store, feature, manifest, Path(entry["plan"]), state.run_id,
                operator=args.operator, reason=args.reason, dry_run=args.dry_run,
            )
        for name in sorted(summary):
            value = summary[name]
            if isinstance(value, list):
                value = ", ".join(str(item) for item in value) or "none"
            print(f"{name}: {value}")
        return 0
    with store.lock(f"feature-{state.slug}"):
        command = args.command
        if command == "next":
            command = {"PLAN": "plan", "RESUME": "resume"}.get(next_action(state), "run")
            if next_action(state) in {"APPROVE", "HUMAN_TEST", "NONE", "RESUME"}:
                action = next_action(state)
                if action == "RESUME":
                    print(f"Human gate: run orch resume {state.slug}")
                else:
                    print(f"Human gate: {action}")
                return 0
        if command == "plan":
            if state.phase not in {"DRAFT", "DISCUSSING", "PLAN_READY"}:
                raise ContractError(f"Cannot plan from {state.phase}")
            plan_feature(store, entry, feature, state)
        elif command == "approve":
            approve_feature(store, entry, feature, state)
        elif command in {"run", "resume"}:
            if command == "resume" and state.phase in {"PAUSED", "ESCALATED"}:
                state.phase = "RUNNING" if state.approved_hashes else (state.previous_phase or "DISCUSSING")
                state.error = None
                state = store.save(state)
                store.event(state.run_id, "resumed")
            if state.phase == "DISCUSSING":
                plan_feature(store, entry, feature, state)
            else:
                target_config = resolution.config
                if target_config.viewer and command == "run":
                    from orch.platform_support import launch_viewer
                    result = launch_viewer(state.slug, data_dir=store.data_dir)
                    print(result.message)
                asyncio.run(engine(store, entry, feature, state, resolution).run(
                    one_stage=args.command == "next"))
        elif command == "done":
            if state.phase != "AWAITING_TEST":
                raise ContractError("done requires AWAITING_TEST")
            state.phase = "DONE"
            store.save(state)
            store.event(state.run_id, "human_test_accepted")
        elif command == "reopen":
            if state.phase not in {"DONE", "AWAITING_TEST"}:
                raise ContractError("reopen requires DONE or AWAITING_TEST")
            notes = args.notes_file.read_text(encoding="utf-8")
            destination = Path(entry["plan"]) / f"reopen-{state.round + 1}.md"
            atomic_write(destination, notes)
            discussion, body = read_markdown(Path(entry["plan"]) / "discussion.md", Discussion)
            discussion.ready = False
            discussion.blocking_questions = [f"Incorporate reopen notes: {destination}"]
            write_markdown(Path(entry["plan"]) / "discussion.md", discussion, body)
            state.phase = "DISCUSSING"
            state.round += 1
            state.stage = "WORK"
            state.approved_hashes = {}
            from orch.git_ops import head
            state.workspace["feature_head"] = head(Path(state.workspace["feature_path"]))
            for data in state.workers.values():
                data["checked"] = False
            store.save(state)
            store.event(state.run_id, "feature_reopened", notes=str(destination))
        elif command == "clean":
            if state.phase not in {"DONE", "ABORTED"}:
                raise ContractError("clean requires DONE or ABORTED; unfinished work is retained")
            from orch.git_ops import GitWorkspace, WorkspaceRecord
            GitWorkspace(feature.target_project, store.run_dir(state.run_id)).clean(
                WorkspaceRecord.from_dict(state.workspace))
            store.event(state.run_id, "cleaned")
        else:
            raise ContractError(f"Unsupported command: {command}")
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return run_command(args)
    except KeyboardInterrupt:
        print("Interrupted; persisted worktrees and run files are retained.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, portalocker.exceptions.LockException) as exc:
        print(f"orch: {exc}", file=sys.stderr)
        return 1
