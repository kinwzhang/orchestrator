"""Command-line lifecycle controls. All feature-specific inputs live in Markdown."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import portalocker

from orch.config import effective_feature, load_config
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
from orch.workflow import Engine, approval_hashes, next_action, validate_plan


def parser():
    root = argparse.ArgumentParser(prog="orch", description="Markdown-driven local delivery")
    root.add_argument("--data-dir", type=Path, help="Override platform state directory")
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
    validate = commands.add_parser("validate", help="Validate a Markdown contract")
    validate.add_argument("kind", choices=["feature", "manifest", "discussion", "handover", "verdict"])
    validate.add_argument("file", type=Path)
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


def context(store, slug):
    entry = locate(store, slug)
    source = Path(entry["plan"]) / "feature.md"
    feature, _ = read_markdown(source, FeatureSpec)
    config = load_config(Path(entry["repo"]))
    feature = effective_feature(feature, config, source)
    state = store.load(entry["run_id"])
    return entry, feature, state


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


def plan_feature(store, entry, feature, state):
    import psutil

    from orch.agents import AgentRequest, CodexAdapter
    from orch.platform_support import executable_argv
    plan = Path(entry["plan"])
    instruction = store.run_dir(state.run_id) / "planning-instruction.md"
    model = feature.models.resolve("planner")
    template = Path(__file__).parent / "templates" / "planner.md"
    body = template.read_text() if template.exists() else "Maintain discussion.md and write all plan artifacts."
    atomic_write(instruction, f"# Planning session\n\nCanonical plan folder: {plan}\nRequirement: {plan / 'requirement.md'}\nDiscussion: {plan / 'discussion.md'}\n\n{body}\n")
    state.phase = "DISCUSSING"
    state = store.save(state)
    model = feature.models.resolve("planner")
    request = AgentRequest(role="planner", logical_id="planner", attempt_id="interactive",
                           instruction_file=instruction, output_file=plan / "plan.md",
                           cwd=Path(state.workspace["feature_path"]), model=model,
                           writable_roots=(plan,))
    argv = CodexAdapter().interactive_argv(request)
    store.event(state.run_id, "dispatch_intent", agent="planner", role="planner", model=model.model)

    def stop_owned_process(process, identity):
        """Stop only the planner process tree whose creation identity was recorded."""
        processes = []
        try:
            parent = psutil.Process(identity["pid"])
            if abs(parent.create_time() - identity["create_time"]) <= 0.01:
                processes = [*parent.children(recursive=True), parent]
        except psutil.NoSuchProcess:
            pass
        for child in reversed(processes):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        try:
            process.terminate()
        except OSError:
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
    info.update(status="running", role="planner", attempt=1, started_at=timestamp())
    state = store.save(state)
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    try:
        process = subprocess.Popen(executable_argv(argv), cwd=request.cwd, **kwargs)
    except OSError:
        info = state.workers["planner"]
        info["status"] = "failed"
        store.save(state)
        raise
    try:
        created = psutil.Process(process.pid).create_time()
    except psutil.NoSuchProcess:
        created = 0.0
    identity = {"pid": process.pid, "create_time": created}
    info = state.workers["planner"]
    info["process"] = identity
    state = store.save(state)
    store.event(state.run_id, "process_started", agent="planner", **identity)

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
        store.event(state.run_id, "planning_interrupted", request=interrupted,
                    exit_code=process.returncode, **identity)
        return state

    store.event(state.run_id, "process_completed", agent="planner", exit_code=process.returncode,
                **identity)
    state.workers["planner"].update(status="done" if process.returncode == 0 else "failed",
                                    exit_code=process.returncode)
    state = store.save(state)
    store.event(state.run_id, "planning_exited", exit_code=process.returncode)
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


def engine(store, entry, feature, state):
    manifest, _ = read_markdown(Path(entry["plan"]) / "manifest.md", Manifest)
    feature.limits = manifest.limits
    from orch.processes import ProcessRunner
    config = load_config(Path(entry["repo"]))
    runner = ProcessRunner([os.environ.get(name, "") for name in config.redaction_env])
    return Engine(store, state, feature, manifest, Path(entry["plan"]), runner=runner, config=config)


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


def run_command(args):
    config = load_config(Path.cwd())
    store = StateStore(args.data_dir or config.data_dir)
    if args.command == "validate":
        schema = {"feature": FeatureSpec, "manifest": Manifest, "discussion": Discussion,
                  "handover": Handover, "verdict": Verdict}[args.kind]
        read_markdown(args.file, schema)
        print(f"Valid {args.kind}: {args.file}")
        return 0
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
                    print(f"  Issue {issue.id}: {issue.status}; root {issue.root_issue_id}; {issue.summary}")
                if state.error:
                    print(f"  Blocker: {state.error}")
        return 0
    entry = locate(store, args.slug)
    if args.command == "watch":
        from orch.watch import watch
        watch(store, entry["run_id"], once=args.once)
        return 0
    entry, feature, state = context(store, args.slug)
    if args.command in {"pause", "abort"}:
        control(store, state, args.command)
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
                target_config = load_config(Path(entry["repo"]))
                if target_config.viewer and command == "run":
                    from orch.platform_support import launch_viewer
                    result = launch_viewer(state.slug, data_dir=store.data_dir)
                    print(result.message)
                asyncio.run(engine(store, entry, feature, state).run(one_stage=args.command == "next"))
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
