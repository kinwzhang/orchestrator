import argparse
import json
from pathlib import Path

from orch.cli import approve_feature, control, new_feature
from orch.contracts import Discussion, FeatureSpec, Manifest, write_markdown
from orch.git_ops import git, head
from orch.store import StateStore, atomic_write


def test_cli_creation_approval_and_pause_recovery(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "requirement.md").write_text("# Requirement\nA change.\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    original = head(repo)
    store = StateStore(tmp_path / "state")
    args = argparse.Namespace(slug="sample", target=repo, requirement=None, base="main")
    with store.lock("feature-sample"):
        state = new_feature(store, args)
    entry = json.loads((store.data_dir / "features/sample.json").read_text())
    plan = Path(entry["plan"])
    feature = FeatureSpec(slug="sample", target_project=repo, requirement=repo / "requirement.md")
    write_markdown(plan / "discussion.md", Discussion(ready=True))
    write_markdown(plan / "manifest.md", Manifest(slug="sample", branch="feature/sample", workers=[{
        "id": "w1", "title": "Task", "instruction": "workers/w1.md", "allowed_paths": ["app.py"]}]))
    for name in ["plan.md", "workers/w1.md", "reviewer.md", "doc-reviewer.md"]:
        atomic_write(plan / name, "# Task\n\n## Interfaces\nA contract.\n")
    state = approve_feature(store, entry, feature, state)
    assert state.phase == "APPROVED"
    assert state.workspace["feature_head"] == head(Path(state.workspace["feature_path"]))
    assert state.workspace["feature_head"] != original
    assert head(repo) == original
    # A dead runner can leave RUNNING on disk; pause still takes effect immediately.
    state.phase = "RUNNING"
    state = store.save(state)
    paused = control(store, state, "pause")
    assert paused.phase == "PAUSED"
    assert not (store.run_dir(state.run_id) / "control.json").exists()
    assert "plan_approved" in (store.run_dir(state.run_id) / "activity_log.md").read_text()


def test_usage_totals_do_not_treat_missing_as_zero():
    from orch.contracts import RunState
    from orch.reporting import render_report
    state = RunState(run_id="test", slug="test")
    report = render_report(state, [
        {"kind": "agent_completed", "usage": {"input_tokens": 10}, "duration_seconds": 1},
        {"kind": "agent_completed", "usage": {"input_tokens": 20}, "duration_seconds": 2},
        {"kind": "agent_completed", "usage": None, "duration_seconds": 1}])
    assert "input_tokens: 30" in report
    assert "2/3" in report
