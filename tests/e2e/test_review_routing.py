"""Independent code/docs review roles, checkpointing and audited batch recovery."""
from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.contracts import (
    ContractError,
    Discussion,
    FeatureSpec,
    Finding,
    Handover,
    IssueState,
    Manifest,
    ModelSpec,
    RoleModels,
    RunState,
    Verdict,
    read_markdown,
    write_markdown,
)
from orch.git_ops import GitWorkspace, git, head
from orch.store import StateStore, atomic_write
from orch.workflow import Engine, Escalation, recover_review_batch

LIVE_RUN = Path("/home/zwj808/.local/state/orch/runs/model-preferences-cli-5ec7f5ee")
LIVE_CANDIDATE = "4f134f315b9ab85e2058452b9895946f579355bd"


class ReviewStub:
    """Stub agent with independent per-role verdict scripts; no live calls."""

    def __init__(self, code_script, docs_script):
        self.calls = []
        self.scripts = {"code": list(code_script), "docs": list(docs_script)}

    @staticmethod
    def candidate_sha(request):
        text = request.instruction_file.read_text(encoding="utf-8")
        match = re.search(r"candidate_sha: ([0-9a-f]{40})", text)
        assert match, "review instruction must bind the candidate SHA"
        return match.group(1)

    async def run(self, request, on_event=None):
        self.calls.append(request)
        cycle = int(next(p.name.removeprefix("round-")
                         for p in request.output_file.parents
                         if p.name.startswith("round-")))
        if request.role in {"worker", "replacement"}:
            (request.cwd / "app.py").write_text("answer = 42\n")
            (request.cwd / "guide.md").write_text("# Answer\n\nRelevant guide.\n")
            write_markdown(request.output_file, Handover(
                agent=request.logical_id, round=cycle, status="done",
                files_changed=["app.py", "guide.md"], docs_changed=["guide.md"],
                checks_run=[]), "## Interfaces and contracts\n\nProvides an answer.\n")
        else:
            role = "code" if request.role == "code_reviewer" else "docs"
            if role == "docs":
                assert all(path.suffix == ".md" for path in request.cwd.iterdir())
                assert not (request.cwd / "app.py").exists()
            kind = self.scripts[role].pop(0) if self.scripts[role] else "pass"
            if kind == "malformed":
                request.output_file.parent.mkdir(parents=True, exist_ok=True)
                request.output_file.write_text("malformed verdict")
            else:
                verdict = self.verdict(kind, role, self.candidate_sha(request))
                write_markdown(request.output_file, verdict, "## Summary\n\nStub.\n")
        return SimpleNamespace(exit_code=0, duration_seconds=0.01, reason=None,
                               usage=None, adapter_version="stub")

    @staticmethod
    def verdict(kind, role, candidate_sha):
        if kind == "pass":
            return Verdict(reviewer=role, result="pass", candidate_sha=candidate_sha)
        if kind == "pass-resolve-code-1":
            assert role == "code"
            return Verdict(reviewer=role, result="pass", candidate_sha=candidate_sha,
                           resolved=["code-1"])
        if kind == "cross-role":
            return Verdict(reviewer=role, result="pass", candidate_sha=candidate_sha,
                           resolved=["code-1"])
        if kind == "rework-code-1":
            return Verdict(reviewer=role, result="rework", candidate_sha=candidate_sha,
                           findings=[Finding(id="code-1", worker="w1", severity="major",
                                             action="rework_same_worker", summary="fix",
                                             root_issue_id="code-1")])
        raise AssertionError(f"unknown stub verdict {kind}")


@pytest.fixture
def routing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    store = StateStore(tmp_path / "state")
    feature = FeatureSpec(
        slug="sample", target_project=repo, requirement=repo / "requirement.md",
        models=RoleModels(**{role: ModelSpec(model="fake/model") for role in (
            "planner", "worker", "code_reviewer", "docs_reviewer", "conflict_resolver")}),
        commit_plan=False,
    )
    workspace = GitWorkspace(repo, store.run_dir("sample-run"))
    record = workspace.prepare(feature)
    plan = record.plan_path
    plan.mkdir(parents=True)
    write_markdown(plan / "feature.md", feature)
    write_markdown(plan / "discussion.md", Discussion(ready=True))
    manifest = Manifest(slug="sample", branch=record.feature_branch, workers=[
        {"id": "w1", "title": "answer", "instruction": "workers/w1.md",
         "allowed_paths": ["app.py", "guide.md"]}])
    write_markdown(plan / "manifest.md", manifest)
    for name, text in {
        "requirement.md": "# Requirement\nReturn an answer.\n",
        "plan.md": "# Plan\n\n## Interfaces\nAnswer.\n",
        "reviewer.md": "# Code review\nCheck acceptance.\n",
        "doc-reviewer.md": "# Docs review\nCheck relevance only.\n",
        "workers/w1.md": "# Worker\nImplement answer.\n",
    }.items():
        atomic_write(plan / name, text)
    workspace.commit_plan(plan)
    state = RunState(
        run_id="sample-run", slug="sample", phase="APPROVED",
        workspace={**record.to_dict(), "feature_head": head(record.feature_path)},
        approved_hashes=__import__("orch.workflow", fromlist=["approval_hashes"]).approval_hashes(
            plan, manifest, feature),
        issues={"code-1": IssueState(id="code-1", worker="w1", root_issue_id="code-1",
                                     owner_role="code")},
    )
    state = store.save(state)
    return store, state, feature, manifest, plan


def run_resume(store, state, feature, manifest, plan, stub):
    state = store.load(state.run_id)
    state.phase = "RUNNING"
    state.error = None
    state = store.save(state)
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": stub})
    asyncio.run(engine.run(one_stage=True))
    return store.load(state.run_id)


def test_code_result_survives_invalid_docs_and_resume_retries_only_docs(routing):
    store, state, feature, manifest, plan = routing
    stub = ReviewStub(code_script=["pass-resolve-code-1"], docs_script=["cross-role", "cross-role"])
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": stub})
    with pytest.raises(Escalation, match="docs"):
        asyncio.run(engine.run())
    rejected = store.load(state.run_id)
    assert rejected.phase == "ESCALATED"
    assert rejected.issues["code-1"].status == "resolved"
    code_results = [result for result in rejected.review_results.values() if result.role == "code"]
    assert len(code_results) == 1 and code_results[0].result == "pass"
    assert not any(result.role == "docs" for result in rejected.review_results.values())
    assert [call.role for call in stub.calls].count("code_reviewer") == 1
    assert [call.role for call in stub.calls].count("docs_reviewer") == 2
    assert rejected.approved_hashes == state.approved_hashes
    worker = rejected.workers["w1"]
    assert worker["attempt"] == 1 and worker["infrastructure_retries"] == 0
    docs_instruction = (store.run_dir(state.run_id) / "round-1" / "inputs" /
                        "review-docs-0.md").read_text(encoding="utf-8")
    assert "role=docs" in docs_instruction
    assert "never treat a worker's statement" in docs_instruction

    stub.scripts["docs"] = ["pass"]
    result = run_resume(store, rejected, feature, manifest, plan, stub)
    assert result.stage == "MERGE"
    assert [call.role for call in stub.calls].count("code_reviewer") == 1
    assert [call.role for call in stub.calls].count("docs_reviewer") == 3
    accepted = list(result.review_results.values())
    assert {entry.role for entry in accepted} == {"code", "docs"}
    assert {entry.candidate_sha for entry in accepted} == {result.candidate["sha"]}
    assert {entry.result for entry in accepted} == {"pass"}
    activity = (store.run_dir(state.run_id) / "activity_log.md").read_text(encoding="utf-8")
    assert "dual_role_review_passed" in activity
    round_dir = store.run_dir(state.run_id) / "round-1"
    assert (round_dir / "verdict-docs-1.md").exists()
    assert (round_dir / "verdict-docs-2.md").exists()
    docs_result = next(entry for entry in accepted if entry.role == "docs")
    assert Path(docs_result.verdict_path).name == "verdict-docs-3.md"


def test_docs_result_survives_invalid_code_and_resume_retries_only_code(routing):
    store, state, feature, manifest, plan = routing
    state.issues.clear()
    state = store.save(state)
    stub = ReviewStub(code_script=["malformed", "malformed"], docs_script=["pass"])
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": stub})
    with pytest.raises(Escalation, match="code"):
        asyncio.run(engine.run())
    rejected = store.load(state.run_id)
    assert [result.role for result in rejected.review_results.values()] == ["docs"]
    assert [call.role for call in stub.calls].count("code_reviewer") == 2
    assert [call.role for call in stub.calls].count("docs_reviewer") == 1

    stub.scripts["code"] = ["pass"]
    result = run_resume(store, rejected, feature, manifest, plan, stub)
    assert result.stage == "MERGE"
    assert [call.role for call in stub.calls].count("code_reviewer") == 3
    assert [call.role for call in stub.calls].count("docs_reviewer") == 1


class OverlapStub(ReviewStub):
    """Reviewer stub that records actual concurrent activity against the cap."""

    def __init__(self, store, run_id):
        super().__init__([], [])
        self.store = store
        self.run_id = run_id
        self.active = 0
        self.max_active = 0
        self.docs_saw_code_result = None
        self.event = asyncio.Event()

    async def run(self, request, on_event=None):
        if request.role not in {"code_reviewer", "docs_reviewer"}:
            return await super().run(request, on_event)
        role = "code" if request.role == "code_reviewer" else "docs"
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if role == "docs" and self.active == 1:
            self.docs_saw_code_result = any(
                result.role == "code"
                for result in self.store.load(self.run_id).review_results.values())
        if self.active == 2:
            self.event.set()
        try:
            await asyncio.wait_for(self.event.wait(), timeout=0.2)
        except TimeoutError:
            pass
        self.calls.append(request)
        write_markdown(request.output_file, Verdict(
            reviewer=role, result="pass", candidate_sha=self.candidate_sha(request),
            resolved=["code-1"] if role == "code" else []), "## Summary\n\nOverlap.\n")
        self.active -= 1
        return SimpleNamespace(exit_code=0, duration_seconds=0.01, reason=None,
                               usage=None, adapter_version="stub")


def _run_overlap(routing, concurrency):
    from orch.contracts import Limits

    store, state, feature, manifest, plan = routing
    manifest = manifest.model_copy(deep=True)
    manifest.limits = Limits(**{**manifest.limits.model_dump(), "concurrency": concurrency})
    stub = OverlapStub(store, state.run_id)
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": stub})
    asyncio.run(engine.run())
    return store.load(state.run_id), stub


def test_independent_reviewers_overlap_when_capacity_allows(routing):
    result, stub = _run_overlap(routing, 4)
    assert stub.max_active == 2
    assert {entry.role for entry in result.review_results.values()} == {"code", "docs"}
    assert result.stage == "NOTIFY"


def test_concurrency_one_serializes_reviews_and_checkpoints_code_first(routing):
    result, stub = _run_overlap(routing, 1)
    assert stub.max_active == 1
    assert stub.docs_saw_code_result is True
    assert {entry.role for entry in result.review_results.values()} == {"code", "docs"}


def test_ambiguous_legacy_ownership_escalates_without_partial_migration(routing):
    store, state, feature, manifest, plan = routing
    state.issues = {
        "legacy-1": IssueState(id="legacy-1", worker="w1", root_issue_id="legacy-1"),
        "legacy-unknown": IssueState(id="legacy-unknown", worker="w1",
                                     root_issue_id="legacy-unknown"),
    }
    state = store.save(state)
    evidence = store.run_dir(state.run_id) / "round-0" / "verdict-code.md"
    write_markdown(evidence, Verdict(
        reviewer="code", result="rework", candidate_sha=LIVE_CANDIDATE,
        findings=[Finding(id="legacy-1", worker="w1", severity="major",
                          action="rework_same_worker", root_issue_id="legacy-1",
                          summary="legacy evidence")],
    ), "## Body\n")
    stub = ReviewStub(code_script=["pass"], docs_script=["pass"])
    engine = Engine(store, state, feature, manifest, plan, adapters={"opencode": stub})
    with pytest.raises(ContractError, match="Cannot determine owner of legacy-unknown"):
        asyncio.run(engine.run())
    rejected = store.load(state.run_id)
    assert rejected.phase == "ESCALATED"
    assert all(issue.owner_role is None for issue in rejected.issues.values())
    assert not any(event.get("kind") == "issue_ownership_resolved"
                   for event in store.events(state.run_id))


def _copy_live_evidence(store: StateStore, run_id: str) -> Path:
    destination = store.run_dir(run_id)
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("state.json", "events.jsonl"):
        shutil.copy2(LIVE_RUN / name, destination / name)
    for pattern in ("round-1/verdict-*.md", "round-2/verdict-*.md",
                    "round-2/docs-review/*-issues.md"):
        for source in LIVE_RUN.glob(pattern):
            target = destination / source.relative_to(LIVE_RUN)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return destination


@pytest.mark.skipif(
    not (LIVE_RUN / "state.json").exists()
    or not Path("/home/zwj808/PycharmProjects/orc2610_bootstrap_workspaces/feature").exists(),
    reason="live model-preferences-cli evidence is not present on this machine",
)
def test_exact_live_partial_batch_recovery_is_audited_and_idempotent(tmp_path, monkeypatch):
    from orch.config import effective_feature, load_config
    from orch.contracts import FeatureSpec as Feature
    live_state = LIVE_RUN / "state.json"
    live_state_hash = hashlib.sha256(live_state.read_bytes()).hexdigest()
    live_events_hash = hashlib.sha256((LIVE_RUN / "events.jsonl").read_bytes()).hexdigest()
    candidate = LIVE_RUN / "worktrees" / "integration-2"
    if head(candidate) != LIVE_CANDIDATE or git(candidate, "status", "--porcelain").strip():
        pytest.skip("live candidate worktree is not clean at the reviewed SHA")

    store = StateStore(tmp_path / "state")
    run_dir = _copy_live_evidence(store, "model-preferences-cli-5ec7f5ee")
    state = store.load("model-preferences-cli-5ec7f5ee")
    plan = Path(state.workspace["feature_path"]) / ".plans" / state.slug
    feature_source = plan / "feature.md"
    feature, _ = read_markdown(feature_source, Feature)
    feature = effective_feature(
        feature, load_config(Path(state.workspace["repo"])), feature_source)
    manifest, _ = read_markdown(plan / "manifest.md", Manifest)
    feature.limits = manifest.limits

    before_state = (run_dir / "state.json").read_bytes()
    summary = recover_review_batch(
        store, feature, manifest, plan, state.run_id,
        operator="repair-test", reason="dry run of the exact live batch", dry_run=True,
    )
    assert summary["status"] == "dry-run"
    assert summary["removed"] == ["code-w01-05"]
    assert summary["invalid_resolved"] == ["code-w01-01", "code-w01-02", "code-w01-03",
                                           "code-w01-04"]
    assert (run_dir / "state.json").read_bytes() == before_state

    class Crash(BaseException):
        pass

    original = store.record_operation

    def crash_after_intent(current, key, value):
        if key.startswith("review-batch-recovery:") and value.get("status") != "completed":
            original(current, key, value)
            raise Crash
        return original(current, key, value)

    monkeypatch.setattr(store, "record_operation", crash_after_intent)
    with pytest.raises(Crash):
        recover_review_batch(
            store, feature, manifest, plan, state.run_id,
            operator="repair-test", reason="recover the live partially applied batch",
        )
    monkeypatch.undo()
    interrupted = store.load(state.run_id)
    key = next(iter(k for k in interrupted.operations if k.startswith("review-batch-recovery:")))
    assert interrupted.operations[key]["status"] == "intent"
    assert interrupted.issues["code-w01-02"].assignments[0].review_rounds == 1

    summary = recover_review_batch(
        store, feature, manifest, plan, state.run_id,
        operator="repair-test", reason="recover the live partially applied batch",
    )
    assert summary["status"] == "recovered"
    recovered = store.load(state.run_id)
    assert recovered.phase == "ESCALATED" and recovered.stage == "REVIEW"
    assert recovered.error is None
    for identity in ("code-w01-01", "code-w01-04"):
        assert recovered.issues[identity].status == "resolved"
    for identity in ("code-w01-02", "code-w01-03"):
        assignment = recovered.issues[identity].assignments[0]
        assert assignment.review_rounds == 1
        assert assignment.failed_repairs == 1
        assert recovered.issues[identity].owner_role == "code"
    new_issue = recovered.issues["code-w01-05"]
    assert new_issue.owner_role == "code" and new_issue.status == "open"
    assert new_issue.assignments[0].review_rounds == 0
    assert recovered.workers["w01"]["attempt"] == 6
    assert recovered.workers["w01"]["infrastructure_retries"] == 0
    assert "worker-commit:w01:de5e48925cc47a9b1f3dcfc1e6789b16909b0beb" in recovered.operations
    code_keys = [result for result in recovered.review_results.values() if result.role == "code"]
    assert len(code_keys) == 1 and code_keys[0].result == "rework"
    assert code_keys[0].binding == "legacy-attempt"
    assert not any(result.role == "docs" for result in recovered.review_results.values())
    events = store.events(state.run_id)
    assert len([event for event in events if event.get("kind") == "review_batch_recovered"]) == 1
    assert len([event for event in events if event.get("kind") == "review_result_rejected"
                and event.get("role") == "docs"]) == 1
    assert recovered.operations[key]["status"] == "completed"

    revision = recovered.revision
    again = recover_review_batch(
        store, feature, manifest, plan, state.run_id,
        operator="another", reason="different arguments must not add a decision",
    )
    assert again["status"] == "already-recovered"
    assert store.load(state.run_id).revision == revision
    assert hashlib.sha256(live_state.read_bytes()).hexdigest() == live_state_hash
    assert hashlib.sha256((LIVE_RUN / "events.jsonl").read_bytes()).hexdigest() == live_events_hash
