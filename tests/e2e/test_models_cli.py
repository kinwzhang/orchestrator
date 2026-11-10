"""End-to-end CLI behaviour for isolated model preference commands."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def env(tmp_path):
    values = dict(os.environ)
    values.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PYTHONPATH": str(ROOT / "src") + os.pathsep + values.get("PYTHONPATH", ""),
        "ORCH_TEST_ISOLATED": "1",
    })
    path = tmp_path / "config" / "orch" / "config.toml"
    return values, path


def run(args, env_values, *, cwd=None, stdin=None, check=False):
    return subprocess.run(
        [sys.executable, "-m", "orch", *args],
        cwd=cwd,
        env=env_values,
        input=stdin,
        text=True,
        capture_output=True,
        check=check,
        timeout=120,
    )


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    return repo


def test_show_outside_repository_lists_unset_and_creates_nothing(tmp_path, env):
    values, config_path = env
    result = run(["models", "show"], values, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    for role in ("planner", "worker", "replacement", "code_reviewer", "docs_reviewer",
                 "conflict_resolver"):
        assert role in result.stdout
    assert "unset" in result.stdout
    assert not config_path.exists()
    assert not (tmp_path / "config").exists()


def test_global_set_then_show_reports_source(tmp_path, env):
    values, config_path = env
    result = run(["models", "set", "worker", "--model", "provider/coder",
                  "--reasoning", "low"], values, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert config_path.is_file()
    shown = run(["models", "show"], values, cwd=tmp_path)
    assert "provider/coder" in shown.stdout
    assert "global" in shown.stdout


def test_repository_set_changes_only_the_target(tmp_path, env):
    values, config_path = env
    run(["models", "set", "worker", "--model", "global/coder", "--reasoning", "low"],
        values, cwd=tmp_path)
    global_before = config_path.read_text()
    repo = make_repo(tmp_path)
    result = run(["models", "set", "worker", "--model", "repo/coder",
                  "--reasoning", "high", "--scope", "repo"], values, cwd=repo)
    assert result.returncode == 0, result.stderr
    assert config_path.read_text() == global_before
    text = (repo / ".orch.toml").read_text()
    assert 'model = "repo/coder"' in text
    assert "global/coder" not in text
    shown = run(["models", "show", "--target", str(repo)], values, cwd=tmp_path)
    assert "repo/coder" in shown.stdout
    assert "repository" in shown.stdout
    assert str(config_path.resolve()) in shown.stdout


def test_partial_repository_override_is_rejected_without_writing(tmp_path, env):
    values, _ = env
    repo = make_repo(tmp_path)
    result = run(["models", "set", "worker", "--reasoning", "low", "--scope", "repo"],
                 values, cwd=repo)
    assert result.returncode != 0
    assert not (repo / ".orch.toml").exists()
    (repo / ".orch.toml").write_text('[models.worker]\nmodel = "repo/coder"\n')
    before = (repo / ".orch.toml").read_bytes()
    result = run(["models", "set", "worker", "--reasoning", "high", "--scope", "repo"],
                 values, cwd=repo)
    assert result.returncode != 0
    assert (repo / ".orch.toml").read_bytes() == before


def test_noninteractive_fallback_exits_and_global_flag_selects_models(tmp_path, env):
    values, config_path = env
    config_path.parent.mkdir(parents=True)
    config_path.write_text('[models.worker]\nmodel = "global/coder"\nreasoning = "low"\n')
    repo = make_repo(tmp_path)
    (repo / ".orch.toml").write_text('[models.worker]\nmodel = "repo/coder"\n')
    before = (repo / ".orch.toml").read_bytes()
    result = run(["models", "show", "--target", str(repo)], values, cwd=tmp_path,
                 stdin="")
    assert result.returncode != 0
    assert "--global-models" in result.stderr
    assert (repo / ".orch.toml").read_bytes() == before
    selected = run(["--global-models", "models", "show", "--target", str(repo)],
                   values, cwd=tmp_path, stdin="")
    assert selected.returncode == 0, selected.stderr
    assert "global/coder" in selected.stdout
    assert "Model selection: global preferences" in selected.stdout


def test_linked_global_config_directory_is_refused_by_cli(tmp_path, env):
    values, _ = env
    real = tmp_path / "real-config"
    real.mkdir()
    try:
        (tmp_path / "config").symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    result = run(["models", "set", "worker", "--model", "provider/coder",
                  "--reasoning", "low"], values, cwd=tmp_path)
    assert result.returncode != 0
    assert "linked configuration path" in result.stderr
    assert not (real / "orch" / "config.toml").exists()


def test_invalid_configuration_diagnostics_never_echo_values(tmp_path, env):
    values, config_path = env
    sentinel = "https://hooks.invalid/very-secret-token"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(f'unknown_setting = "{sentinel}"\n')
    for args in (["models", "show"],
                 ["models", "set", "worker", "--model", "provider/coder",
                  "--reasoning", "low"]):
        result = run(args, values, cwd=tmp_path)
        assert result.returncode != 0
        assert sentinel not in result.stdout
        assert sentinel not in result.stderr
        # Undeclared keys are input, so not even the key name is reproduced.
        assert "unknown_setting" not in result.stdout
        assert "unknown_setting" not in result.stderr
        assert "extra_forbidden" in result.stderr
        assert "config.toml" in result.stderr
    assert config_path.read_text() == f'unknown_setting = "{sentinel}"\n'


def test_extra_key_under_known_role_never_appears_in_cli_output(tmp_path, env):
    values, config_path = env
    sentinel = "https://hooks.invalid/very-secret-token"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('[models.worker]\nmodel = "provider/coder"\n'
                           f'reasoning = "low"\n"{sentinel}" = "leak"\n')
    before = config_path.read_bytes()
    for args in (["models", "show"],
                 ["models", "set", "worker", "--model", "other/coder",
                  "--reasoning", "high"]):
        result = run(args, values, cwd=tmp_path)
        assert result.returncode != 0
        assert sentinel not in result.stdout
        assert sentinel not in result.stderr
        assert "models.worker" in result.stderr
        assert "extra_forbidden" in result.stderr
        assert "config.toml" in result.stderr
    assert config_path.read_bytes() == before


def test_repository_role_extra_key_never_appears_in_cli_output(tmp_path, env):
    values, config_path = env
    sentinel = "https://hooks.invalid/very-secret-token"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('[models.worker]\nmodel = "global/coder"\nreasoning = "low"\n')
    repo = make_repo(tmp_path)
    repo_file = repo / ".orch.toml"
    repo_file.write_text('[models.worker]\nmodel = "repo/coder"\n'
                         f'reasoning = "high"\n"{sentinel}" = "leak"\n')
    before = repo_file.read_bytes()
    shown = run(["models", "show"], values, cwd=repo)
    assert shown.returncode != 0
    assert "consent" in shown.stderr
    assert sentinel not in shown.stdout and sentinel not in shown.stderr
    written = run(["models", "set", "worker", "--model", "repo/other",
                   "--reasoning", "low", "--scope", "repo"], values, cwd=repo)
    assert written.returncode != 0
    assert sentinel not in written.stdout and sentinel not in written.stderr
    assert repo_file.read_bytes() == before



def test_unknown_repository_role_key_never_appears_in_cli_output(tmp_path, env):
    values, _ = env
    repo = make_repo(tmp_path)
    sentinel = "https://hooks.invalid/very-secret-token"
    config = repo / ".orch.toml"
    config.write_text(f'[models."{sentinel}"]\nmodel = "x"\nreasoning = "y"\n')
    before = config.read_bytes()
    result = run(["models", "show", "--target", str(repo)], values, cwd=tmp_path)
    assert result.returncode != 0
    assert sentinel not in result.stdout
    assert sentinel not in result.stderr
    assert "unknown model role" in result.stderr
    assert config.read_bytes() == before


def test_global_flag_is_rejected_with_models_set(tmp_path, env):
    values, config_path = env
    result = run(["--global-models", "models", "set", "worker", "--model", "x",
                  "--reasoning", "low"], values, cwd=tmp_path)
    assert result.returncode != 0
    assert not config_path.exists()


@pytest.mark.parametrize("command", [
    ["new", "demo"],
    ["status"],
    ["watch", "demo"],
    ["pause", "demo"],
    ["abort", "demo"],
    ["done", "demo"],
    ["clean", "demo"],
])
def test_global_flag_is_rejected_for_commands_without_model_selection(
        tmp_path, env, command):
    values, config_path = env
    result = run(["--global-models", *command], values, cwd=tmp_path)
    assert result.returncode != 0
    assert "--global-models" in result.stderr
    assert not config_path.exists()


def test_global_flag_is_announced_before_lifecycle_action(tmp_path, env):
    values, config_path = env
    config_path.parent.mkdir(parents=True)
    config_path.write_text('[models.worker]\nmodel = "global/coder"\nreasoning = "low"\n')
    repo = make_repo(tmp_path)
    (repo / "app.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test",
                    "-c", "user.email=test@example.invalid", "commit", "-q",
                    "-m", "initial"], check=True)
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "feature.md").write_text(
        "---\nslug: demo\ntarget_project: " + str(repo) + "\n"
        "requirement: requirement.md\n---\n")
    (plan / "requirement.md").write_text("# Requirement\n")
    (plan / "discussion.md").write_text(
        "---\nready: true\nblocking_questions: []\n---\n# Discussion\n")
    data_dir = tmp_path / "state-data"
    features = data_dir / "features"
    features.mkdir(parents=True)
    (features / "demo.json").write_text(json.dumps({
        "slug": "demo", "run_id": "demo-1", "repo": str(repo),
        "plan": str(plan), "status": "ready"}))
    run_dir = data_dir / "runs" / "demo-1"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({
        "version": 1, "run_id": "demo-1", "slug": "demo", "phase": "DONE",
        "workspace": {"feature_path": str(repo), "feature_branch": "feature/demo"}}))
    notes = tmp_path / "notes.md"
    notes.write_text("# Notes\n")
    result = run(["--data-dir", str(data_dir), "--global-models", "reopen", "demo",
                  str(notes)], values, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Model selection: global preferences" in result.stdout
    assert "--global-models" in result.stdout


@pytest.mark.parametrize("spelling", ["--global-model", "--global"])
def test_global_flag_abbreviations_are_rejected(tmp_path, env, spelling):
    values, config_path = env
    result = run([spelling, "models", "show"], values, cwd=tmp_path)
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr
    assert not config_path.exists()


def test_invalid_feature_front_matter_never_echoes_values(tmp_path, env):
    values, _ = env
    repo = make_repo(tmp_path)
    sentinel = "https://hooks.invalid/feature-secret-token"
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "feature.md").write_text(
        "---\nslug: " + sentinel + "\ntarget_project: " + str(repo) + "\n"
        "requirement: requirement.md\n---\n")
    data_dir = tmp_path / "state-data"
    features = data_dir / "features"
    features.mkdir(parents=True)
    (features / "demo.json").write_text(
        '{"slug": "demo", "run_id": "demo-1", "repo": "' + str(repo) + '", '
        '"plan": "' + str(plan) + '", "status": "ready"}')
    result = run(["--data-dir", str(data_dir), "models", "show", "--feature", "demo"],
                 values, cwd=tmp_path)
    assert result.returncode != 0
    assert sentinel not in result.stdout
    assert sentinel not in result.stderr
    assert "slug" in result.stderr


@pytest.mark.parametrize("front_matter, expected", [
    ("slug: demo\ncredential-secret-value: [broken", "line 2, column"),
    ("slug: demo\nworker: !credential-secret-value value", "line 2, column"),
    ("slug: demo\ncredential-secret-value: a\ncredential-secret-value: b",
     "Duplicate or non-string YAML key"),
    ("slug: demo\nmodels: {worker: [credential-secret-value}", "line 2, column"),
])
def test_malformed_feature_yaml_never_echoes_source(tmp_path, env, front_matter, expected):
    values, config_path = env
    repo = make_repo(tmp_path)
    plan = tmp_path / "plan"
    plan.mkdir()
    feature_path = plan / "feature.md"
    feature_path.write_text("---\n" + front_matter + "\n---\n")
    data_dir = tmp_path / "state-data"
    features = data_dir / "features"
    features.mkdir(parents=True)
    (features / "demo.json").write_text(json.dumps({
        "slug": "demo", "run_id": "demo-1", "repo": str(repo),
        "plan": str(plan), "status": "ready"}))
    before = feature_path.read_bytes()
    result = run(["--data-dir", str(data_dir), "models", "show", "--feature", "demo"],
                 values, cwd=tmp_path)
    assert result.returncode != 0
    assert "credential-secret-value" not in result.stdout
    assert "credential-secret-value" not in result.stderr
    assert str(feature_path) in result.stderr
    assert expected in result.stderr
    assert feature_path.read_bytes() == before
    assert not config_path.exists()


def test_global_flag_is_rejected_for_validate(tmp_path, env):
    values, _ = env
    path = tmp_path / "discussion.md"
    path.write_text("---\nready: true\nblocking_questions: []\n---\n")
    result = run(["--global-models", "validate", "discussion", str(path)],
                 values, cwd=tmp_path)
    assert result.returncode != 0


def test_show_and_set_do_not_touch_registry_state_or_events(tmp_path, env):
    values, _ = env
    data_dir = tmp_path / "state-data"
    features = data_dir / "features"
    features.mkdir(parents=True)
    entry = features / "demo.json"
    entry.write_text('{"slug": "demo", "run_id": "demo-1", "repo": "/tmp/x", '
                     '"plan": "/tmp/x/.plans/demo", "status": "ready"}')
    event_log = data_dir / "runs" / "demo-1" / "events.jsonl"
    event_log.parent.mkdir(parents=True)
    event_log.write_text('{"kind": "existing"}\n')
    before = {path: path.read_bytes() for path in (entry, event_log)}
    run(["--data-dir", str(data_dir), "models", "set", "worker", "--model", "p/c",
         "--reasoning", "low"], values, cwd=tmp_path)
    run(["--data-dir", str(data_dir), "models", "show"], values, cwd=tmp_path)
    assert all(path.read_bytes() == data for path, data in before.items())


def test_modules_never_import_agent_or_workflow_machinery(tmp_path, env):
    values, _ = env
    probe = (
        "import sys, orch.models\n"
        "assert 'orch.agents' not in sys.modules, 'agents imported'\n"
        "assert 'orch.workflow' not in sys.modules, 'workflow imported'\n"
        "assert 'orch.processes' not in sys.modules, 'processes imported'\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], env=values, cwd=tmp_path,
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_concurrent_cli_writers_with_different_data_dirs_keep_both(tmp_path, env):
    values, config_path = env
    args = [
        [sys.executable, "-m", "orch", "--data-dir", str(tmp_path / "data-a"),
         "models", "set", "worker", "--model", "provider/worker", "--reasoning", "low"],
        [sys.executable, "-m", "orch", "--data-dir", str(tmp_path / "data-b"),
         "models", "set", "planner", "--model", "provider/planner", "--reasoning", "low"],
    ]
    processes = [subprocess.Popen(command, env=values, cwd=tmp_path,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for command in args]
    for process in processes:
        _, error = process.communicate(timeout=120)
        assert process.returncode == 0, error.decode()
    text = config_path.read_text()
    assert "provider/worker" in text
    assert "provider/planner" in text


def test_feature_selector_uses_registered_canonical_feature(tmp_path, env):
    values, config_path = env
    config_path.parent.mkdir(parents=True)
    config_path.write_text('[models.worker]\nmodel = "global/coder"\nreasoning = "low"\n')
    repo = make_repo(tmp_path)
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "feature.md").write_text(
        "---\nslug: demo\ntarget_project: " + str(repo) + "\n"
        "requirement: requirement.md\n"
        "models:\n  worker:\n    model: feature/coder\n---\n")
    data_dir = tmp_path / "state-data"
    features = data_dir / "features"
    features.mkdir(parents=True)
    (features / "demo.json").write_text(
        '{"slug": "demo", "run_id": "demo-1", "repo": "' + str(repo) + '", '
        '"plan": "' + str(plan) + '", "status": "ready"}')
    result = run(["--data-dir", str(data_dir), "models", "show", "--feature", "demo"],
                 values, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "feature/coder" in result.stdout
    assert "feature" in result.stdout


def test_ambiguous_selectors_fail(tmp_path, env):
    values, _ = env
    result = run(["models", "show", "--target", str(tmp_path), "--feature", "demo"],
                 values, cwd=tmp_path)
    assert result.returncode != 0


def test_unknown_role_and_option_are_rejected(tmp_path, env):
    values, config_path = env
    result = run(["models", "set", "sorcerer", "--model", "x"], values, cwd=tmp_path)
    assert result.returncode != 0
    assert not config_path.exists()
    result = run(["models", "set", "worker", "--modl", "x"], values, cwd=tmp_path)
    assert result.returncode != 0
    assert not config_path.exists()
