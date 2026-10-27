import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.contracts import (
    Discussion,
    FeatureSpec,
    Handover,
    Manifest,
    ModelSpec,
    RoleModels,
    RunState,
    Verdict,
    write_markdown,
)
from orch.git_ops import GitWorkspace, git, head
from orch.store import StateStore, atomic_write
from orch.workflow import ApprovalRequired, Engine, Escalation, approval_hashes


class FakeAgents:
    def __init__(self):
        self.calls = []

    async def run(self, request, on_event=None):
        self.calls.append(request)
        if request.role in {"worker", "replacement"}:
            (request.cwd / "app.py").write_text("answer = 42\n")
            (request.cwd / "guide.md").write_text("# Answer\n\nThe answer is available.\n")
            write_markdown(request.output_file, Handover(
                agent=request.logical_id, round=1, status="done", files_changed=["app.py", "guide.md"],
                docs_changed=["guide.md"], checks_run=[]), "## Interfaces and contracts\n\nProvides an answer.\n")
        else:
            role = "code" if request.role == "code_reviewer" else "docs"
            if role == "docs":
                assert not (request.cwd / "app.py").exists()
                assert all(p.suffix == ".md" for p in request.cwd.iterdir())
            write_markdown(request.output_file, Verdict(reviewer=role, result="pass"), "## Summary\n\nPass.\n")
        return SimpleNamespace(exit_code=0, duration_seconds=0.01, reason=None,
                               usage=None, adapter_version="fake-1")


@pytest.fixture
def delivery(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    store = StateStore(tmp_path / "state")
    feature = FeatureSpec(slug="sample", target_project=repo, requirement=repo / "requirement.md",
                          models=RoleModels(**{role: ModelSpec(model="fake/model") for role in (
                              "planner", "worker", "code_reviewer", "docs_reviewer", "conflict_resolver")}))
    workspace = GitWorkspace(repo, store.run_dir("sample-run"))
    record = workspace.prepare(feature)
    plan = record.plan_path
    plan.mkdir(parents=True)
    write_markdown(plan / "feature.md", feature)
    write_markdown(plan / "discussion.md", Discussion(ready=True))
    manifest = Manifest(slug="sample", branch=record.feature_branch, workers=[
        {"id": "w1", "title": "answer", "instruction": "workers/w1.md", "allowed_paths": ["app.py", "guide.md"]}])
    write_markdown(plan / "manifest.md", manifest)
    for name, text in {"requirement.md": "# Requirement\nReturn an answer.\n", "plan.md": "# Plan\n\n## Interfaces\nAnswer.\n",
                       "reviewer.md": "# Code review\nCheck acceptance.\n", "doc-reviewer.md": "# Docs review\nCheck relevance only.\n",
                       "workers/w1.md": "# Worker\nImplement answer.\n"}.items():
        atomic_write(plan / name, text)
    workspace.commit_plan(plan)
    state = RunState(run_id="sample-run", slug="sample", phase="APPROVED", workspace={
        **record.to_dict(), "feature_head": head(record.feature_path)}, approved_hashes=approval_hashes(plan, manifest, feature))
    state = store.save(state)
    return store, state, feature, manifest, plan


def test_complete_delivery_reviews_candidate_leaves_main_untouched(delivery):
    store, state, feature, manifest, plan = delivery
    original = head(feature.target_project)
    fake = FakeAgents()
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": fake})
    asyncio.run(engine.run())
    result = store.load(state.run_id)
    assert result.phase == "AWAITING_TEST"
    assert head(feature.target_project) == original
    assert (Path(result.workspace["feature_path"]) / "app.py").exists()
    assert len(fake.calls) == 3
    assert (store.run_dir(state.run_id) / "checklist.md").exists()
    assert "dispatch_intent" in (store.run_dir(state.run_id) / "activity_log.md").read_text()
    assert (plan / "report.md").exists()


def test_edit_invalidates_approval_before_dispatch(delivery):
    store, state, feature, manifest, plan = delivery
    (plan / "workers/w1.md").write_text("changed")
    fake = FakeAgents()
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": fake})
    with pytest.raises(ApprovalRequired):
        asyncio.run(engine.run())
    assert not fake.calls
    assert store.load(state.run_id).phase == "PLAN_READY"


def test_effective_model_change_invalidates_approval(delivery):
    store, state, feature, manifest, plan = delivery
    feature.models.worker = ModelSpec(model="fake/changed")
    fake = FakeAgents()
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": fake})
    with pytest.raises(ApprovalRequired):
        asyncio.run(engine.run())
    assert not fake.calls
    assert store.load(state.run_id).phase == "PLAN_READY"


@pytest.mark.parametrize("change", ["runtime", "instruction"])
def test_failed_attempt_rechecks_approval_before_retry(delivery, change):
    store, state, feature, manifest, plan = delivery

    class MutatingFailure:
        calls = 0

        async def run(self, request, on_event=None):
            self.calls += 1
            if change == "runtime":
                feature._runtime_settings = {"viewer": "changed-after-approval"}
            else:
                (plan / "workers/w1.md").write_text("Changed during failed call\n")
            return SimpleNamespace(exit_code=17, duration_seconds=0.01,
                                   reason="simulated failure", usage=None,
                                   adapter_version="failure-stub")

    fake = MutatingFailure()
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": fake})
    instruction = plan / "workers/w1.md"
    output = store.run_dir(state.run_id) / "round-1" / "handover" / "w1.md"
    with pytest.raises(ApprovalRequired):
        asyncio.run(engine.dispatch("worker", "w1", instruction, output,
                                    Path(state.workspace["feature_path"])))
    assert fake.calls == 1
    assert store.load(state.run_id).phase == "PLAN_READY"


def test_next_does_not_implicitly_resume_paused_feature(delivery, monkeypatch, capsys):
    from orch.cli import main

    store, state, feature, _, plan = delivery
    state.phase = "PAUSED"
    state.previous_phase = "RUNNING"
    store.save(state)
    entry = {"slug": state.slug, "run_id": state.run_id,
             "repo": str(feature.target_project), "plan": str(plan)}
    monkeypatch.setattr("orch.cli.registry", lambda _store: [entry])
    assert main(["--data-dir", str(store.data_dir), "next", state.slug]) == 0
    assert "orch resume sample" in capsys.readouterr().out
    assert store.load(state.run_id).phase == "PAUSED"


def test_step_resume_skips_completed_worker(delivery):
    store, state, feature, manifest, plan = delivery
    fake = FakeAgents()
    for _ in range(2):
        state = store.load(state.run_id)
        engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": fake})
        asyncio.run(engine.run(one_stage=True))
    assert len(fake.calls) == 1
    state = store.load(state.run_id)
    assert state.stage == "CHECK"
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": fake})
    asyncio.run(engine.run())
    assert len(fake.calls) == 3


class ReworkAgents(FakeAgents):
    def __init__(self, malformed_verdict=False, malformed_handover=False):
        super().__init__()
        self.malformed_verdict = malformed_verdict
        self.malformed_handover = malformed_handover
        self.rejected = False

    async def run(self, request, on_event=None):
        from orch.contracts import Finding
        self.calls.append(request)
        cycle = int(next(p.name.removeprefix("round-") for p in request.output_file.parents if p.name.startswith("round-")))
        if request.role in {"worker", "replacement"}:
            if self.malformed_handover:
                self.malformed_handover = False
                request.output_file.parent.mkdir(parents=True, exist_ok=True)
                request.output_file.write_text("malformed")
                return SimpleNamespace(exit_code=0, duration_seconds=0, usage=None, adapter_version="fake")
            if not (request.cwd / "app.py").exists():
                (request.cwd / "app.py").write_text("answer = 42\n")
                changed = ["app.py", "guide.md"]
            else:
                changed = ["guide.md"]
            (request.cwd / "guide.md").write_text(f"# Answer\n\nRelevant handover for cycle {cycle}.\n")
            write_markdown(request.output_file, Handover(agent="w1", round=cycle, status="done",
                files_changed=changed, docs_changed=["guide.md"], checks_run=[]), "## Interfaces and contracts\n\nProvides answer.\n")
        else:
            role = "code" if request.role == "code_reviewer" else "docs"
            if self.malformed_verdict and role == "code":
                self.malformed_verdict = False
                request.output_file.parent.mkdir(parents=True, exist_ok=True)
                request.output_file.write_text("malformed verdict")
            elif role == "docs" and not self.rejected:
                self.rejected = True
                write_markdown(request.output_file, Verdict(reviewer="docs", result="rework", findings=[
                    Finding(id="docs-relevance", worker="w1", root_issue_id="docs-relevance", severity="major",
                            action="rework_same_worker", summary="Make handover relevant")]), "## Findings\n\nExplain the assigned task.\n")
            else:
                write_markdown(request.output_file, Verdict(reviewer=role, result="pass",
                    resolved=["docs-relevance"] if cycle > 1 and role == "docs" else []))
        return SimpleNamespace(exit_code=0, duration_seconds=0, usage=None, adapter_version="fake")


def test_docs_rework_and_malformed_verdict_repaired_once(delivery):
    store, state, feature, manifest, plan = delivery
    fake = ReworkAgents(malformed_verdict=True)
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": fake})
    asyncio.run(engine.run())
    result = store.load(state.run_id)
    assert result.phase == "AWAITING_TEST"
    assert result.round == 2
    assert result.issues["docs-relevance"].status == "resolved"
    assert len([c for c in fake.calls if c.role == "code_reviewer"]) == 3
    assert "rework_issued" in (store.run_dir(state.run_id) / "activity_log.md").read_text()


def test_malformed_handover_returns_to_worker_before_review(delivery):
    store, state, feature, manifest, plan = delivery
    fake = ReworkAgents(malformed_handover=True)
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": fake})
    asyncio.run(engine.run())
    result = store.load(state.run_id)
    assert result.phase == "AWAITING_TEST"
    assert result.issues["check-w1"].status == "resolved"
    assert [c.role for c in fake.calls[:2]] == ["worker", "worker"]


def test_pause_cancels_agent_and_retains_partial_uncommitted_work(delivery):
    store, state, feature, manifest, plan = delivery
    started = []
    class SlowAgent:
        async def run(self, request, on_event=None):
            (request.cwd / "unfinished.txt").write_text("retain me")
            started.append(request)
            atomic_write(store.run_dir(state.run_id) / "control.json", '{"request": "pause"}')
            await asyncio.sleep(10)
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": SlowAgent()})
    asyncio.run(engine.run())
    result = store.load(state.run_id)
    assert result.phase == "PAUSED"
    assert (started[0].cwd / "unfinished.txt").read_text() == "retain me"
    assert result.workers["w1"]["status"] == "interrupted"


@pytest.mark.parametrize("control_request,phase", [("pause", "PAUSED"), ("abort", "ABORTED")])
def test_interactive_planner_observes_pause_and_abort(delivery, monkeypatch, control_request, phase):
    from orch.agents import CodexAdapter
    from orch.cli import control, plan_feature

    store, state, feature, _, plan = delivery
    entry = {"plan": str(plan), "repo": str(feature.target_project)}
    monkeypatch.setattr(
        CodexAdapter,
        "interactive_argv",
        lambda self, agent_request: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    completed = []
    failures = []

    def planning_thread():
        try:
            with store.lock(f"feature-{state.slug}"):
                completed.append(plan_feature(store, entry, feature, state))
        except (OSError, ValueError, RuntimeError) as exc:
            failures.append(exc)

    thread = threading.Thread(target=planning_thread)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(event["kind"] == "process_started" and event.get("agent") == "planner"
               for event in store.events(state.run_id)):
            break
        time.sleep(0.02)
    else:
        pytest.fail("interactive planner did not start")

    control(store, state, control_request)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not failures
    result = store.load(state.run_id)
    assert result.phase == phase
    assert result.previous_phase == "DISCUSSING"
    assert result.workers["planner"]["status"] == "interrupted"
    planner_attempt = result.workers["planner"]["attempt_id"]
    assert any(event["kind"] == "planning_interrupted" and event.get("attempt") == planner_attempt
               for event in store.events(state.run_id))
    assert any(event["kind"] == "process_completed" and event.get("attempt") == planner_attempt
               for event in store.events(state.run_id))


class PlannerCrash(BaseException):
    pass


class PlannerPopenStub:
    def __init__(self, pid, returncode=None):
        self.pid = pid
        self.returncode = returncode
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1


def _planner_cli(monkeypatch, store, feature, plan):
    from orch.cli import main

    entry = {"slug": feature.slug, "run_id": "sample-run",
             "repo": str(feature.target_project), "plan": str(plan)}
    monkeypatch.setattr("orch.cli.registry", lambda _store: [entry])

    def invoke(command):
        return main(["--data-dir", str(store.data_dir), command, feature.slug])

    return invoke


def _crash_after_planner_popen(store, state, feature, plan, monkeypatch):
    from orch.agents import CodexAdapter
    from orch.cli import plan_feature

    state.phase = "DISCUSSING"
    state.approved_hashes = {}
    state = store.save(state)
    launches = []
    stub = PlannerPopenStub(pid=os.getpid())
    launched = {"called": False}

    def popen(*args, **kwargs):
        launched["called"] = True
        launches.append(stub)
        return stub

    monkeypatch.setattr(CodexAdapter, "interactive_argv",
                        lambda self, request: [sys.executable, "-c", "pass"])
    monkeypatch.setattr("orch.cli.subprocess.Popen", popen)
    save = store.save

    def crash_before_identity_is_durable(next_state, expected_revision=None):
        planner = next_state.workers.get("planner", {})
        if (launched["called"]
                and planner.get("process_attempt_id") == planner.get("attempt_id")):
            raise PlannerCrash("crash after Popen and before process identity save")
        return save(next_state, expected_revision)

    monkeypatch.setattr(store, "save", crash_before_identity_is_durable)
    entry = {"plan": str(plan), "repo": str(feature.target_project)}
    with pytest.raises(PlannerCrash):
        plan_feature(store, entry, feature, state)
    monkeypatch.setattr(store, "save", save)
    return launches, stub


def test_planner_crash_after_popen_preserves_unresolved_attempt(delivery, monkeypatch):
    store, state, feature, _, plan = delivery
    launches, process = _crash_after_planner_popen(store, state, feature, plan, monkeypatch)

    assert len(launches) == 1
    interrupted = store.load(state.run_id)
    attempt = interrupted.workers["planner"]["attempt_id"]
    events = store.events(state.run_id)
    assert interrupted.workers["planner"]["status"] == "running"
    assert sum(event["kind"] == "dispatch_intent" and event.get("attempt") == attempt
               for event in events) == 1
    assert not any(event["kind"] == "process_started" and event.get("attempt") == attempt
                   for event in events)
    assert process.terminate_calls == process.kill_calls == 0


def test_plan_and_resume_refuse_duplicate_after_uncertain_planner_crash(delivery, monkeypatch, capsys):
    store, state, feature, _, plan = delivery
    launches, process = _crash_after_planner_popen(store, state, feature, plan, monkeypatch)
    invoke = _planner_cli(monkeypatch, store, feature, plan)
    planning_files = list(store.run_dir(state.run_id).glob("planning-instruction-*.md"))
    assert len(planning_files) == 1
    original_instruction = planning_files[0].read_text(encoding="utf-8")
    codex_argv_calls = []
    from orch.agents import CodexAdapter
    monkeypatch.setattr(CodexAdapter, "interactive_argv",
                        lambda self, request: codex_argv_calls.append(request) or [sys.executable, "-c", "pass"])

    assert invoke("plan") == 1
    first_error = capsys.readouterr().err
    assert "no matching durable process identity" in first_error
    assert store.load(state.run_id).phase == "ESCALATED"
    assert invoke("resume") == 1
    second_error = capsys.readouterr().err
    assert "no matching durable process identity" in second_error
    assert store.load(state.run_id).phase == "ESCALATED"
    assert len(launches) == 1
    assert not codex_argv_calls
    assert process.terminate_calls == process.kill_calls == 0
    assert list(store.run_dir(state.run_id).glob("planning-instruction-*.md")) == planning_files
    assert planning_files[0].read_text(encoding="utf-8") == original_instruction
    assert sum(event["kind"] == "dispatch_intent" and event.get("agent") == "planner"
               for event in store.events(state.run_id)) == 1


def test_stale_planner_identity_from_another_attempt_is_not_reused(delivery, monkeypatch, capsys):
    store, state, feature, _, plan = delivery
    state.phase = "DISCUSSING"
    state.approved_hashes = {}
    old_attempt, current_attempt = "planner-1-1", "planner-1-2"
    state.workers["planner"] = {
        "status": "running", "attempt": 2, "attempt_id": current_attempt,
        "process_attempt_id": old_attempt,
        "process": {"pid": os.getpid(), "create_time": 1.0},
    }
    state = store.save(state)
    store.event(state.run_id, "dispatch_intent", agent="planner", attempt=old_attempt)
    store.event(state.run_id, "process_started", agent="planner", attempt=old_attempt,
                pid=987654321, create_time=1.0)
    store.event(state.run_id, "process_completed", agent="planner", attempt=old_attempt,
                pid=987654321, create_time=1.0, exit_code=0)
    store.event(state.run_id, "planning_exited", agent="planner", attempt=old_attempt,
                exit_code=0)
    store.event(state.run_id, "dispatch_intent", agent="planner", attempt=current_attempt)

    invoke = _planner_cli(monkeypatch, store, feature, plan)
    launches = []
    monkeypatch.setattr("orch.cli.subprocess.Popen",
                        lambda *args, **kwargs: launches.append(args) or None)
    assert invoke("plan") == 1
    assert "no matching durable process identity" in capsys.readouterr().err
    assert not launches
    assert store.load(state.run_id).phase == "ESCALATED"


def test_completed_planner_attempt_allows_fresh_session(delivery, monkeypatch):
    store, state, feature, _, plan = delivery
    state.phase = "DISCUSSING"
    state.approved_hashes = {}
    old_attempt = "planner-1-1"
    state.workers["planner"] = {
        "status": "done", "attempt": 1, "attempt_id": old_attempt,
        "process_attempt_id": old_attempt,
        "process": {"pid": 987654321, "create_time": 1.0},
    }
    state = store.save(state)
    store.event(state.run_id, "dispatch_intent", agent="planner", attempt=old_attempt)
    store.event(state.run_id, "process_started", agent="planner", attempt=old_attempt,
                pid=987654321, create_time=1.0)
    store.event(state.run_id, "process_completed", agent="planner", attempt=old_attempt,
                pid=987654321, create_time=1.0, exit_code=0)
    store.event(state.run_id, "planning_exited", agent="planner", attempt=old_attempt,
                exit_code=0)

    from orch.agents import CodexAdapter
    from orch.cli import plan_feature
    monkeypatch.setattr(CodexAdapter, "interactive_argv",
                        lambda self, request: [sys.executable, "-c", "pass"])
    process = PlannerPopenStub(pid=os.getpid(), returncode=0)
    launches = []
    monkeypatch.setattr("orch.cli.subprocess.Popen",
                        lambda *args, **kwargs: launches.append(args) or process)
    entry = {"plan": str(plan), "repo": str(feature.target_project)}

    result = plan_feature(store, entry, feature, state)
    assert len(launches) == 1
    assert result.phase == "PLAN_READY"
    assert result.workers["planner"]["attempt_id"] == "planner-1-2"
    assert any(event["kind"] == "planning_exited" and event.get("attempt") == "planner-1-2"
               for event in store.events(state.run_id))


def _completing_planner(monkeypatch):
    from orch.agents import CodexAdapter

    launches = []
    process = PlannerPopenStub(pid=os.getpid(), returncode=0)
    monkeypatch.setattr(CodexAdapter, "interactive_argv",
                        lambda self, request: [sys.executable, "-c", "pass"])
    monkeypatch.setattr("orch.cli.subprocess.Popen",
                        lambda *args, **kwargs: launches.append(args) or process)
    return launches, process


def test_successive_planner_sessions_use_distinct_increasing_ids(delivery, monkeypatch):
    from orch.cli import plan_feature

    store, state, feature, _, plan = delivery
    state.phase = "DISCUSSING"
    state.approved_hashes = {}
    state = store.save(state)
    entry = {"plan": str(plan), "repo": str(feature.target_project)}
    launches, _ = _completing_planner(monkeypatch)

    attempts = []
    ids = []
    for _ in range(3):
        state = plan_feature(store, entry, feature, store.load(state.run_id))
        attempts.append(state.workers["planner"]["attempt"])
        ids.append(state.workers["planner"]["attempt_id"])

    assert attempts == [1, 2, 3]
    assert ids == ["planner-1-1", "planner-1-2", "planner-1-3"]
    assert len(set(ids)) == len(ids) == 3

    # A fresh store instance models a process restart: the persisted counter
    # must continue rather than resetting to one.
    reloaded = StateStore(store.data_dir).load(state.run_id)
    assert reloaded.workers["planner"]["attempt"] == 3
    assert reloaded.workers["planner"]["attempt_id"] == "planner-1-3"
    state = plan_feature(store, entry, feature, reloaded)
    assert state.workers["planner"]["attempt"] == 4
    assert state.workers["planner"]["attempt_id"] == "planner-1-4"
    assert len(launches) == 4


def test_later_planner_crash_is_not_masked_by_earlier_completion(delivery, monkeypatch, capsys):
    from orch.agents import CodexAdapter
    from orch.cli import plan_feature

    store, state, feature, _, plan = delivery
    state.phase = "DISCUSSING"
    state.approved_hashes = {}
    state = store.save(state)
    entry = {"plan": str(plan), "repo": str(feature.target_project)}
    _completing_planner(monkeypatch)
    for _ in range(2):
        state = plan_feature(store, entry, feature, store.load(state.run_id))
    assert state.workers["planner"]["attempt_id"] == "planner-1-2"

    # Crash after Popen on the third dispatch, before its identity is durable.
    launches, process = _crash_after_planner_popen(
        store, store.load(state.run_id), feature, plan, monkeypatch)
    assert len(launches) == 1
    crashed = store.load(state.run_id)
    assert crashed.workers["planner"]["attempt_id"] == "planner-1-3"
    assert crashed.workers["planner"]["status"] == "running"

    invoke = _planner_cli(monkeypatch, store, feature, plan)
    argv_calls = []
    monkeypatch.setattr(CodexAdapter, "interactive_argv",
                        lambda self, request: argv_calls.append(request) or [sys.executable, "-c", "pass"])
    assert invoke("plan") == 1
    assert "refusing to launch Codex" in capsys.readouterr().err
    assert store.load(state.run_id).phase == "ESCALATED"
    assert invoke("resume") == 1
    assert "refusing to launch Codex" in capsys.readouterr().err
    assert store.load(state.run_id).phase == "ESCALATED"

    events = store.events(state.run_id)
    dispatched = [event.get("attempt") for event in events
                  if event["kind"] == "dispatch_intent" and event.get("agent") == "planner"]
    assert dispatched == ["planner-1-1", "planner-1-2", "planner-1-3"]
    assert len(launches) == 1
    assert not argv_calls
    assert process.terminate_calls == process.kill_calls == 0


def test_repeated_legacy_planner_dispatch_ids_fail_closed(delivery, monkeypatch, capsys):
    from orch.agents import CodexAdapter

    store, state, feature, _, plan = delivery
    state.phase = "DISCUSSING"
    state.approved_hashes = {}
    attempt = "planner-1-1"
    state.workers["planner"] = {
        "status": "running", "attempt": 1, "attempt_id": attempt,
        "process_attempt_id": attempt,
        "process": {"pid": os.getpid(), "create_time": 1.0},
    }
    state = store.save(state)
    store.event(state.run_id, "dispatch_intent", agent="planner", attempt=attempt)
    store.event(state.run_id, "process_started", agent="planner", attempt=attempt,
                pid=987654321, create_time=1.0)
    store.event(state.run_id, "process_completed", agent="planner", attempt=attempt,
                pid=987654321, create_time=1.0, exit_code=0)
    store.event(state.run_id, "planning_exited", agent="planner", attempt=attempt, exit_code=0)
    # Corrupted/legacy journal reuses the ID; the old completion must not
    # resolve this later intent.
    store.event(state.run_id, "dispatch_intent", agent="planner", attempt=attempt)

    invoke = _planner_cli(monkeypatch, store, feature, plan)
    launches = []
    monkeypatch.setattr(CodexAdapter, "interactive_argv",
                        lambda self, request: launches.append(request) or [sys.executable, "-c", "pass"])
    monkeypatch.setattr("orch.cli.subprocess.Popen",
                        lambda *args, **kwargs: launches.append(args) or None)

    assert invoke("plan") == 1
    assert "repeats in persisted evidence" in capsys.readouterr().err
    assert store.load(state.run_id).phase == "ESCALATED"
    assert invoke("resume") == 1
    assert "repeats in persisted evidence" in capsys.readouterr().err
    assert store.load(state.run_id).phase == "ESCALATED"
    assert not launches
    assert sum(event["kind"] == "dispatch_intent" and event.get("agent") == "planner"
               for event in store.events(state.run_id)) == 2


def test_planner_completion_before_later_intent_does_not_count(delivery, monkeypatch, capsys):
    from orch.agents import CodexAdapter

    store, state, feature, _, plan = delivery
    state.phase = "DISCUSSING"
    state.approved_hashes = {}
    attempt = "planner-1-1"
    state.workers["planner"] = {"status": "running", "attempt": 1, "attempt_id": attempt}
    state = store.save(state)
    # Corrupted ordering: completion evidence predates the intent it must resolve.
    store.event(state.run_id, "process_started", agent="planner", attempt=attempt,
                pid=987654321, create_time=1.0)
    store.event(state.run_id, "process_completed", agent="planner", attempt=attempt,
                pid=987654321, create_time=1.0, exit_code=0)
    store.event(state.run_id, "dispatch_intent", agent="planner", attempt=attempt)

    invoke = _planner_cli(monkeypatch, store, feature, plan)
    launches = []
    monkeypatch.setattr(CodexAdapter, "interactive_argv",
                        lambda self, request: launches.append(request) or [sys.executable, "-c", "pass"])
    monkeypatch.setattr("orch.cli.subprocess.Popen",
                        lambda *args, **kwargs: launches.append(args) or None)

    assert invoke("plan") == 1
    assert "no matching durable process identity" in capsys.readouterr().err
    assert store.load(state.run_id).phase == "ESCALATED"
    assert invoke("resume") == 1
    assert "no matching durable process identity" in capsys.readouterr().err
    assert not launches


def test_crash_after_worker_commit_reconciles_before_checkpoint(delivery):
    store, state, feature, manifest, plan = delivery
    fake = FakeAgents()
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": fake})
    asyncio.run(engine.run(one_stage=True))  # PREPARE
    state = store.load(state.run_id)
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": fake})
    commit = engine.git.commit_worker

    class SimulatedCrash(BaseException):
        pass

    def commit_then_crash(*args, **kwargs):
        commit(*args, **kwargs)
        raise SimulatedCrash("power loss after commit")

    engine.git.commit_worker = commit_then_crash
    with pytest.raises(SimulatedCrash):
        asyncio.run(engine.run_worker(manifest.workers[0]))

    interrupted = store.load(state.run_id)
    assert interrupted.workers["w1"].get("commit_pending")
    assert interrupted.operations[f"worker-commit:w1:{engine.record.workers['w1'].baseline_sha}"]["status"] == "intent"
    resumed = Engine(store, interrupted, feature, manifest, plan, adapters={"opencode": fake})
    asyncio.run(resumed.run_worker(manifest.workers[0]))
    recovered = store.load(state.run_id)
    assert recovered.workers["w1"]["checked"] is True
    assert recovered.workers["w1"]["status"] == "done"
    operation = next(value for key, value in recovered.operations.items() if key.startswith("worker-commit:w1:"))
    assert operation["status"] == "completed"
    assert operation["reconciled"] is True


def test_spawn_before_identity_event_escalates_without_redispatch(delivery, monkeypatch):
    from orch.processes import ProcessRunner

    store, state, feature, manifest, plan = delivery
    spawned = []

    class SimulatedCrash(BaseException):
        pass

    class SimulatedProcess:
        pid = 987654321

    async def simulated_spawn(*args, **kwargs):
        spawned.append(True)
        return SimulatedProcess()

    def identity_lookup(_pid):
        raise SimulatedCrash("crash after spawn, before identity persistence")

    runner = ProcessRunner()

    class ProcessAdapter:
        async def run(self, request, on_event=None):
            return await runner.run(
                [sys.executable, "-c", "pass"], request.cwd, timeout=1,
                log_paths=(request.log_dir / "stdout.log", request.log_dir / "stderr.log"),
                on_event=on_event,
            )

    engine = Engine(store, state, feature, manifest, plan,
                    adapters={"opencode": ProcessAdapter()})
    instruction = plan / "workers/w1.md"
    output = store.run_dir(state.run_id) / "round-1" / "handover" / "w1.md"
    with monkeypatch.context() as patch:
        patch.setattr("orch.processes.asyncio.create_subprocess_exec", simulated_spawn)
        patch.setattr("orch.processes.psutil.Process", identity_lookup)
        with pytest.raises(SimulatedCrash):
            asyncio.run(engine.dispatch("worker", "w1", instruction, output,
                                        Path(state.workspace["feature_path"])))

    assert spawned == [True]
    after_crash = store.load(state.run_id)
    attempt = after_crash.workers["w1"]["attempt_id"]
    assert after_crash.workers["w1"]["status"] == "running"
    assert any(event["kind"] == "dispatch_intent" and event.get("attempt") == attempt
               for event in store.events(state.run_id))
    assert not any(event["kind"] == "process_started" and event.get("attempt") == attempt
                   for event in store.events(state.run_id))

    resumed_adapter = FakeAgents()
    resumed = Engine(store, after_crash, feature, manifest, plan,
                     adapters={"opencode": resumed_adapter})
    with pytest.raises(Escalation, match="no durable process identity"):
        asyncio.run(resumed.reconcile_processes())
    assert not resumed_adapter.calls


def test_stale_process_identity_from_prior_attempt_cannot_authorize_recovery(delivery):
    store, state, feature, manifest, plan = delivery
    current_attempt = "w1-1-2"
    old_attempt = "w1-1-1"
    state.workers["w1"] = {
        "status": "running", "attempt": 2, "attempt_id": current_attempt,
        "process_attempt_id": old_attempt,
        "process": {"pid": 987654321, "create_time": 1.0},
    }
    state = store.save(state)
    store.event(state.run_id, "dispatch_intent", agent="w1", attempt=old_attempt)
    store.event(state.run_id, "process_started", agent="w1", attempt=old_attempt,
                pid=987654321, create_time=1.0)
    store.event(state.run_id, "process_completed", agent="w1", attempt=old_attempt,
                pid=987654321, create_time=1.0, exit_code=1)
    store.event(state.run_id, "dispatch_intent", agent="w1", attempt=current_attempt)

    engine = Engine(store, store.load(state.run_id), feature, manifest, plan,
                    adapters={"opencode": FakeAgents()})
    with pytest.raises(Escalation, match="no durable process identity"):
        asyncio.run(engine.reconcile_processes())


def test_matching_attempt_process_completion_allows_safe_recovery(delivery):
    store, state, feature, manifest, plan = delivery
    attempt = "w1-1-1"
    state.workers["w1"] = {"status": "running", "attempt": 1, "attempt_id": attempt}
    state = store.save(state)
    store.event(state.run_id, "dispatch_intent", agent="w1", attempt=attempt)
    store.event(state.run_id, "process_started", agent="w1", attempt=attempt,
                pid=987654321, create_time=1.0)
    store.event(state.run_id, "process_completed", agent="w1", attempt=attempt,
                pid=987654321, create_time=1.0, exit_code=1)

    engine = Engine(store, store.load(state.run_id), feature, manifest, plan,
                    adapters={"opencode": FakeAgents()})
    asyncio.run(engine.reconcile_processes())
    recovered = store.load(state.run_id)
    assert recovered.workers["w1"]["status"] == "interrupted"
    assert recovered.workers["w1"]["attempt_id"] == attempt


def test_recovery_escalates_if_leader_exited_before_descendants_were_observed(delivery, monkeypatch):
    store, state, feature, manifest, plan = delivery
    attempt = "w1-1-1"
    state.workers["w1"] = {"status": "running", "attempt": 1, "attempt_id": attempt}
    state = store.save(state)
    store.event(state.run_id, "dispatch_intent", agent="w1", attempt=attempt)
    store.event(state.run_id, "process_started", agent="w1", attempt=attempt,
                pid=987654321, create_time=1.0)
    monkeypatch.setattr("orch.processes.reconcile_events", lambda _events: [{
        "pid": 987654321, "create_time": 1.0, "children": [], "live": [], "status": "exited",
    }])
    adapter = FakeAgents()
    engine = Engine(store, store.load(state.run_id), feature, manifest, plan,
                    adapters={"opencode": adapter})

    with pytest.raises(Escalation, match="descendant ownership was not recorded"):
        asyncio.run(engine.reconcile_processes())
    assert not adapter.calls


def test_post_check_worker_mutation_blocks_candidate_and_returns_to_work(delivery):
    store, state, feature, manifest, plan = delivery
    fake = FakeAgents()
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": fake})
    asyncio.run(engine.run(one_stage=True))  # PREPARE
    state = store.load(state.run_id)
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": fake})
    asyncio.run(engine.run(one_stage=True))  # WORK and worker commit
    state = store.load(state.run_id)
    worker_path = Path(state.workspace["workers"]["w1"]["path"])
    with (worker_path / "app.py").open("a", encoding="utf-8") as stream:
        stream.write("# changed after check\n")

    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": fake})
    asyncio.run(engine.run(one_stage=True))
    result = store.load(state.run_id)
    assert result.stage == "WORK"
    assert result.workers["w1"]["checked"] is False
    assert result.issues["post-check-w1"].status == "open"
    assert not result.candidate
    assert "changed after check" in (worker_path / "app.py").read_text()


def test_sibling_worker_cancellation_and_exception_group_message(delivery):
    store, state, feature, manifest, plan = delivery
    manifest = Manifest.model_validate({
        **manifest.model_dump(),
        "workers": [
            {"id": "w2", "title": "slow sibling", "instruction": "workers/w2.md",
             "allowed_paths": ["sibling/**"]},
            {"id": "w1", "title": "failure", "instruction": "workers/w1.md",
             "allowed_paths": ["app.py", "guide.md"]},
        ],
    })
    atomic_write(plan / "workers/w2.md", "# Slow sibling\n")
    write_markdown(plan / "manifest.md", manifest)
    from orch.git_ops import WorkspaceRecord
    record = WorkspaceRecord.from_dict(state.workspace)
    workspace = GitWorkspace(feature.target_project, store.run_dir(state.run_id))
    workspace.prepare_workers(record, manifest)
    state.workspace = {**state.workspace, **record.to_dict()}
    state.approved_hashes = approval_hashes(plan, manifest, feature)
    state = store.save(state)

    started = asyncio.Event()

    class SiblingFailureAgents:
        async def run(self, request, on_event=None):
            if request.logical_id == "w2":
                started.set()
                await asyncio.sleep(30)
            await asyncio.wait_for(started.wait(), timeout=2)
            raise RuntimeError("precise worker failure detail")

    engine = Engine(store, state, feature, manifest, plan,
                    adapters={"opencode": SiblingFailureAgents()})
    with pytest.raises(Escalation, match="precise worker failure detail"):
        asyncio.run(engine.run())
    result = store.load(state.run_id)
    assert "precise worker failure detail" in result.error
    assert "unhandled errors in a TaskGroup" not in result.error
    assert result.workers["w2"]["status"] == "interrupted"
