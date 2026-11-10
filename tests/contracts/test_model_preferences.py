"""Model preference resolution, provenance and safe configuration writes."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.config import ROLE_NAMES, global_config_path, resolve_config
from orch.contracts import (
    ConflictError,
    ContractError,
    FeatureSpec,
    ModelSpec,
    RoleModels,
    preference_value,
)
from orch.models import (
    consent_resolution,
    inspect_preferences,
    run_models,
    update_preferences,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def global_file(home: Path) -> Path:
    return home / "config" / "orch" / "config.toml"


def repo_dir(home: Path) -> Path:
    repo = home / "repo"
    repo.mkdir(exist_ok=True)
    return repo


def test_show_outside_repo_with_missing_config_lists_unset_and_writes_nothing(home):
    resolution = resolve_config(None)
    view = inspect_preferences(resolution)
    assert [row.role for row in view.rows] == list(ROLE_NAMES)
    assert all(row.model is None for row in view.rows)
    assert all(row.reasoning is None for row in view.rows)
    assert all(row.model_source == "unset" for row in view.rows)
    assert not global_file(home).exists()
    assert not (home / "config").exists()


def test_global_set_creates_minimal_file_and_show_reports_source(home):
    path = global_file(home)
    result = update_preferences(path=path, role="worker", model="provider/coder",
                                reasoning="low")
    assert result.file == path.resolve()
    assert result.updates == {"model": "provider/coder", "reasoning": "low"}
    text = path.read_text()
    assert "[models.worker]" in text
    assert "planner" not in text
    view = inspect_preferences(resolve_config(None))
    worker = next(row for row in view.rows if row.role == "worker")
    assert (worker.model, worker.reasoning) == ("provider/coder", "low")
    assert worker.model_source == "global"
    assert worker.reasoning_source == "global"
    replacement = next(row for row in view.rows if row.role == "replacement")
    assert replacement.model == "provider/coder"
    assert replacement.model_source == "worker_inheritance"


def test_update_preserves_unrelated_content_comments_and_permissions(home):
    path = global_file(home)
    path.parent.mkdir(parents=True)
    path.write_text(
        "# operator comment\n"
        'data_dir = "state"\n'
        "\n"
        "[models.planner]\n"
        'model = "provider/planner"  # keep this\n'
        'reasoning = "low"\n'
        "\n"
        "[limits]\n"
        "concurrency = 2\n"
        "\n"
        "# trailing note\n"
    )
    path.chmod(0o640)
    update_preferences(path=path, role="docs_reviewer", model="provider/reviewer",
                       reasoning="medium")
    text = path.read_text()
    assert "# operator comment" in text
    assert 'model = "provider/planner"  # keep this' in text
    assert 'data_dir = "state"' in text
    assert "# trailing note" in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    config = resolve_config(None).config
    assert config.models.docs_reviewer.model == "provider/reviewer"
    assert config.data_dir == (path.parent / "state").resolve()


def test_repository_override_requires_a_complete_pair_and_falls_back_when_partial(home):
    path = global_file(home)
    path.parent.mkdir(parents=True)
    path.write_text('[models.worker]\nmodel = "global/coder"\nreasoning = "low"\n')
    repo = repo_dir(home)
    (repo / ".orch.toml").write_text('[models.worker]\nmodel = "repo/coder"\n')
    resolution = resolve_config(repo)
    assert [(finding.role, finding.resolved_source) for finding in resolution.findings] == [
        ("worker", "global")]
    assert resolution.config.models.worker.model == "global/coder"
    (repo / ".orch.toml").write_text(
        '[models.worker]\nmodel = "repo/coder"\nreasoning = "high"\n')
    resolution = resolve_config(repo)
    assert not resolution.findings
    assert resolution.config.models.worker.model == "repo/coder"
    worker = next(row for row in inspect_preferences(resolution).rows if row.role == "worker")
    assert worker.model_source == "repository"
    assert worker.model_path == (repo / ".orch.toml").resolve()


def test_unknown_roles_and_broken_toml_name_the_file(home):
    repo = repo_dir(home)
    (repo / ".orch.toml").write_text('[models.sorcerer]\nmodel = "x"\nreasoning = "y"\n')
    with pytest.raises(ContractError, match=r"\.orch\.toml.*unknown model role"):
        resolve_config(repo)
    (repo / ".orch.toml").write_text("[models\n")
    with pytest.raises(ContractError, match=r"\.orch\.toml.*invalid TOML"):
        resolve_config(repo)
    broken_global = global_file(home)
    broken_global.parent.mkdir(parents=True, exist_ok=True)
    broken_global.write_text("[models\n")
    with pytest.raises(ContractError, match=r"config\.toml.*invalid TOML"):
        resolve_config(None)


def test_unknown_repository_role_key_is_never_echoed(home):
    repo = repo_dir(home)
    sentinel = "https://hooks.invalid/very-secret-token"
    (repo / ".orch.toml").write_text(
        f'[models."{sentinel}"]\nmodel = "x"\nreasoning = "y"\n')
    before = (repo / ".orch.toml").read_bytes()
    with pytest.raises(ContractError) as caught:
        resolve_config(repo)
    message = str(caught.value)
    assert sentinel not in message
    assert ".orch.toml" in message
    assert "unknown model role" in message
    assert (repo / ".orch.toml").read_bytes() == before

    global_path = global_file(home)
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(f'[models."{sentinel}"]\nmodel = "x"\n')
    with pytest.raises(ContractError) as caught:
        resolve_config(None)
    assert sentinel not in str(caught.value)
    assert "unknown model role" in str(caught.value)


def test_preference_values_reject_c1_control_characters(home):
    for value in ("model\x85", "model\u009f", "low\x00", "reasoning\x1b"):
        with pytest.raises(ValueError, match="control characters"):
            preference_value(value, "model")
    repo = repo_dir(home)
    target = repo / ".orch.toml"
    target.write_text('[models.worker]\nmodel = "provider/coder"\nreasoning = "low"\n')
    before = target.read_bytes()
    with pytest.raises(ContractError, match="control characters"):
        update_preferences(path=target, role="worker", model="repo/coder\x85",
                           reasoning="high", repo=repo)
    assert target.read_bytes() == before


def test_invalid_unrelated_settings_remain_fatal(home):
    repo = repo_dir(home)
    (repo / ".orch.toml").write_text("[limits]\nconcurrency = 99\n")
    with pytest.raises(ContractError, match=r"\.orch\.toml"):
        resolve_config(repo)


def test_replacement_repository_declaration_is_inherit_plus_reasoning(home):
    path = global_file(home)
    path.parent.mkdir(parents=True)
    path.write_text(
        '[models.worker]\nmodel = "global/coder"\nreasoning = "low"\n'
        '[models.replacement]\ninherit = "worker"\nreasoning = "high"\n')
    repo = repo_dir(home)
    (repo / ".orch.toml").write_text(
        '[models.replacement]\ninherit = "worker"\nreasoning = "medium"\n')
    resolution = resolve_config(repo)
    assert not resolution.findings
    replacement = next(
        row for row in inspect_preferences(resolution).rows if row.role == "replacement")
    assert replacement.model == "global/coder"
    assert replacement.reasoning == "medium"
    assert replacement.model_source == "worker_inheritance"
    assert replacement.reasoning_source == "repository"

    (repo / ".orch.toml").write_text(
        '[models.replacement]\nmodel = "repo/coder"\nreasoning = "medium"\n')
    resolution = resolve_config(repo)
    assert resolution.findings[0].role == "replacement"
    assert resolution.config.models.replacement.reasoning == "high"


def test_repository_one_field_update_keeps_the_explicit_companion(home):
    repo = repo_dir(home)
    update_preferences(path=repo / ".orch.toml", role="worker",
                       model="repo/coder", reasoning="low", repo=repo)
    update_preferences(path=repo / ".orch.toml", role="worker",
                       reasoning="high", repo=repo)
    text = (repo / ".orch.toml").read_text()
    assert 'model = "repo/coder"' in text
    assert 'reasoning = "high"' in text
    update_preferences(path=repo / ".orch.toml", role="worker",
                       model="repo/other", repo=repo)
    text = (repo / ".orch.toml").read_text()
    assert 'model = "repo/other"' in text
    assert 'reasoning = "high"' in text


def test_new_partial_repository_or_global_role_is_rejected_without_writing(home):
    repo = repo_dir(home)
    path = repo / ".orch.toml"
    with pytest.raises(ContractError, match="both --model and --reasoning"):
        update_preferences(path=path, role="worker", reasoning="low", repo=repo)
    assert not path.exists()
    global_path = global_file(home)
    with pytest.raises(ContractError, match=r"models\.planner\.model"):
        update_preferences(path=global_path, role="planner", reasoning="low")
    assert not global_path.exists()


def test_global_reasoning_update_keeps_an_existing_model(home):
    path = global_file(home)
    update_preferences(path=path, role="planner", model="provider/planner")
    update_preferences(path=path, role="planner", reasoning="high")
    text = path.read_text()
    assert 'model = "provider/planner"' in text
    assert 'reasoning = "high"' in text


def test_replacement_cannot_set_a_model(home):
    path = global_file(home)
    with pytest.raises(ContractError, match="inherits the worker model"):
        update_preferences(path=path, role="replacement", model="rogue/model")
    assert not path.exists()


def test_clearing_repository_replacement_removes_the_override_as_a_unit(home):
    path = global_file(home)
    path.parent.mkdir(parents=True)
    path.write_text(
        '[models.worker]\nmodel = "global/coder"\nreasoning = "low"\n'
        '[models.replacement]\ninherit = "worker"\nreasoning = "high"\n')
    repo = repo_dir(home)
    target = repo / ".orch.toml"
    update_preferences(path=target, role="replacement", reasoning="medium", repo=repo)
    assert "replacement" in target.read_text()
    update_preferences(path=target, role="replacement", reasoning="inherit", repo=repo)
    assert "replacement" not in target.read_text()
    replacement = next(
        row for row in inspect_preferences(resolve_config(repo)).rows
        if row.role == "replacement")
    assert replacement.reasoning == "high"
    assert replacement.reasoning_source == "global"


def test_clearing_global_replacement_reasoning_exposes_worker_reasoning(home):
    path = global_file(home)
    path.parent.mkdir(parents=True)
    path.write_text(
        '[models.worker]\nmodel = "global/coder"\nreasoning = "low"\n'
        '[models.replacement]\ninherit = "worker"\nreasoning = "high"\n')
    update_preferences(path=path, role="replacement", reasoning="inherit")
    text = path.read_text()
    assert 'inherit = "worker"' in text
    assert "high" not in text
    view = inspect_preferences(resolve_config(None))
    replacement = next(row for row in view.rows if row.role == "replacement")
    assert replacement.reasoning == "low"
    assert replacement.reasoning_source == "worker_inheritance"


def test_invalid_values_and_reserved_inherit_are_rejected(home):
    path = global_file(home)
    for model in ("", "   ", "bad\nvalue", "bad\x00value"):
        with pytest.raises(ContractError):
            update_preferences(path=path, role="worker", model=model)
    with pytest.raises(ContractError, match="inherit is reserved"):
        update_preferences(path=path, role="worker", model="provider/coder",
                           reasoning="inherit")
    assert not path.exists()
    with pytest.raises(ContractError, match="at least one"):
        update_preferences(path=path, role="worker")


def test_linked_write_paths_are_refused(home, tmp_path):
    real = home / "real.toml"
    real.write_text('[models.worker]\nmodel = "x"\nreasoning = "low"\n')
    linked = home / "config" / "orch" / "config.toml"
    linked.parent.mkdir(parents=True)
    try:
        linked.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ContractError, match="linked configuration path"):
        update_preferences(path=linked, role="planner", model="y", reasoning="low")

    linked_parent = home / "linked-parent"
    try:
        linked_parent.symlink_to(home / "config", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(ContractError, match="linked configuration path"):
        update_preferences(path=linked_parent / "orch" / "config.toml",
                           role="planner", model="y", reasoning="low")


def test_linked_global_directory_and_lock_file_are_refused(home):
    real_config = home / "real-config"
    real_config.mkdir()
    linked_root = home / "config"
    try:
        linked_root.symlink_to(real_config, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")

    target = global_config_path()
    assert target.parent.is_symlink() or target.parent.parent.is_symlink()
    with pytest.raises(ContractError, match="linked configuration path"):
        update_preferences(path=target, role="worker", model="provider/coder",
                           reasoning="low")
    assert not (real_config / "orch" / "config.toml").exists()

    # A linked lock file must be rejected before it is opened, even when the
    # configuration file itself is a normal readable path.
    repo = home / "plain-repo"
    repo.mkdir()
    plain = repo / ".orch.toml"
    plain.write_text('[models.worker]\nmodel = "provider/coder"\nreasoning = "low"\n')
    redirect = home / "redirected.lock"
    redirect.write_text("not a lock\n")
    lock = plain.with_name(f"{plain.name}.models.lock")
    try:
        lock.symlink_to(redirect)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    before = plain.read_bytes()
    with pytest.raises(ContractError, match="linked configuration lock path"):
        update_preferences(path=plain, role="worker", reasoning="high", repo=repo)
    assert plain.read_bytes() == before


def test_validation_errors_never_reproduce_configured_values(home):
    path = global_file(home)
    path.parent.mkdir(parents=True)
    sentinel = "https://hooks.invalid/very-secret-token"
    path.write_text(f'unknown_setting = "{sentinel}"\n')
    before = path.read_bytes()
    with pytest.raises(ContractError) as caught:
        resolve_config(None)
    message = str(caught.value)
    assert sentinel not in message
    # The undeclared key is input, not schema, so even its name is withheld.
    assert "unknown_setting" not in message
    assert "extra_forbidden" in message
    assert "config.toml" in message
    assert path.read_bytes() == before


def test_extra_key_under_known_role_is_never_echoed(home):
    """An extra key inside a declared role table must not reach any output."""
    sentinel = "https://hooks.invalid/very-secret-token"
    path = global_file(home)
    path.parent.mkdir(parents=True)
    path.write_text('[models.worker]\nmodel = "provider/coder"\nreasoning = "low"\n'
                    f'"{sentinel}" = "leak"\n')
    before = path.read_bytes()
    with pytest.raises(ContractError) as caught:
        resolve_config(None)
    message = str(caught.value)
    assert sentinel not in message
    assert "models.worker" in message
    assert "extra_forbidden" in message
    assert "config.toml" in message
    with pytest.raises(ContractError) as caught:
        update_preferences(path=path, role="worker", model="other/coder",
                           reasoning="high", repo=None)
    assert sentinel not in str(caught.value)
    assert path.read_bytes() == before

    path.write_text('[models.worker]\nmodel = "global/coder"\nreasoning = "low"\n')
    repo = repo_dir(home)
    repo_file = repo / ".orch.toml"
    repo_file.write_text('[models.worker]\nmodel = "repo/coder"\nreasoning = "high"\n'
                         f'"{sentinel}" = "leak"\n')
    repo_before = repo_file.read_bytes()
    with pytest.raises(ContractError) as caught:
        update_preferences(path=repo_file, role="worker", model="repo/other",
                           reasoning="low", repo=repo)
    assert sentinel not in str(caught.value)
    assert ".orch.toml" in str(caught.value)
    assert repo_file.read_bytes() == repo_before
    resolution = resolve_config(repo)
    assert resolution.findings
    assert sentinel not in " ".join(finding.reason for finding in resolution.findings)
    assert sentinel not in repr(inspect_preferences(resolution))



def test_fallback_consent_shows_resolved_preferences_before_the_prompt(home, monkeypatch,
                                                                     capsys):
    path = global_file(home)
    path.parent.mkdir(parents=True)
    path.write_text('[models.worker]\nmodel = "global/coder"\nreasoning = "low"\n')
    repo = repo_dir(home)
    (repo / ".orch.toml").write_text('[models.worker]\nmodel = "repo/coder"\n')

    import orch.models as models_module
    monkeypatch.setattr(models_module, "_interactive", lambda: True)
    shown = {}

    def answer(prompt):
        shown["prompt"] = prompt
        shown["before"] = capsys.readouterr().out
        return "n"

    monkeypatch.setattr("builtins.input", answer)
    with pytest.raises(ContractError, match="Declined"):
        consent_resolution(repo)
    assert "Proceed using global preferences" in shown["prompt"]
    assert "global/coder" in shown["before"]
    assert "global" in shown["before"]
    assert "worker" in shown["before"]


def test_show_fallback_denial_prints_choices_and_never_dispatches(home, monkeypatch,
                                                                  capsys):
    path = global_file(home)
    path.parent.mkdir(parents=True)
    path.write_text('[models.worker]\nmodel = "global/coder"\nreasoning = "low"\n')
    repo = repo_dir(home)
    (repo / ".orch.toml").write_text('[models.worker]\nmodel = "repo/coder"\n')
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)

    import orch.models as models_module
    monkeypatch.setattr(models_module, "_interactive", lambda: True)
    before = {}

    def answer(prompt):
        before["output"] = capsys.readouterr().out
        return "n"

    monkeypatch.setattr("builtins.input", answer)
    args = SimpleNamespace(models_command="show", target=repo, feature=None,
                           data_dir=None, global_models=False)
    with pytest.raises(ContractError, match="Declined"):
        run_models(args)
    assert "global/coder" in before["output"]
    assert "Global preferences are being used" in before["output"]


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL preservation")
def test_windows_replacement_preserves_existing_acl(home):
    path = global_file(home)
    path.parent.mkdir(parents=True)
    path.write_text('[models.worker]\nmodel = "provider/coder"\nreasoning = "low"\n')

    def acl():
        return subprocess.run(["icacls", str(path)], capture_output=True,
                              text=True, check=True).stdout

    username = os.environ.get("USERNAME", "")
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                    f"{username}:F"], capture_output=True, check=True)
    before = acl()
    update_preferences(path=path, role="worker", reasoning="high")
    assert "reasoning = \"high\"" in path.read_text()
    assert acl() == before


@pytest.mark.skipif(os.name != "nt", reason="native Windows reparse refusal")
def test_windows_junction_parent_is_refused(home):
    real = home / "real"
    real.mkdir()
    junction = home / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(real)],
                   capture_output=True, check=True)
    with pytest.raises(ContractError, match="linked configuration path"):
        update_preferences(path=junction / "config.toml", role="worker",
                           model="provider/coder", reasoning="low")
    assert not (real / "config.toml").exists()


def test_external_edit_is_detected_and_not_overwritten(home):
    path = global_file(home)
    update_preferences(path=path, role="worker", model="provider/coder", reasoning="low")
    original = path.read_text()

    def external_edit():
        path.write_text(original + '\n[models.planner]\nmodel = "other/planner"\n'
                        'reasoning = "high"\n')

    with pytest.raises(ConflictError, match="changed while editing"):
        update_preferences(path=path, role="worker", reasoning="high",
                           _barrier=external_edit)
    assert 'model = "other/planner"' in path.read_text()
    assert 'reasoning = "low"' in path.read_text()


def test_interruption_before_commit_leaves_the_old_bytes(home):
    path = global_file(home)
    update_preferences(path=path, role="worker", model="provider/coder", reasoning="low")
    original = path.read_bytes()

    def interrupt():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        update_preferences(path=path, role="worker", reasoning="high",
                           _barrier=interrupt)
    assert path.read_bytes() == original


def test_lock_failure_preserves_bytes(home, monkeypatch):
    import portalocker

    import orch.models as models_module

    path = global_file(home)
    update_preferences(path=path, role="worker", model="provider/coder", reasoning="low")
    original = path.read_bytes()

    def refuse_lock(stream, flags):
        raise portalocker.exceptions.LockException("busy")

    monkeypatch.setattr(models_module.portalocker, "lock", refuse_lock)
    monkeypatch.setattr(models_module, "LOCK_TIMEOUT_SECONDS", 0)
    with pytest.raises(Exception, match="lock is unavailable"):
        update_preferences(path=path, role="worker", reasoning="high")
    assert path.read_bytes() == original


CONCURRENT_WRITER = """
import sys
from pathlib import Path
from orch.models import update_preferences

update_preferences(path=Path(sys.argv[1]), role=sys.argv[2],
                   model=f"provider/{sys.argv[2]}", reasoning="low")
"""


def test_concurrent_process_writers_keep_both_updates(home):
    path = global_file(home)
    env = dict(os.environ)
    source_root = Path(__file__).resolve().parents[2] / "src"
    env["PYTHONPATH"] = str(source_root) + os.pathsep + env.get("PYTHONPATH", "")
    processes = [
        subprocess.Popen([sys.executable, "-c", CONCURRENT_WRITER, str(path), role],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for role in ("worker", "planner")
    ]
    for process in processes:
        _, error = process.communicate(timeout=60)
        assert process.returncode == 0, error.decode()
    config = resolve_config(None).config
    assert config.models.worker.model == "provider/worker"
    assert config.models.planner.model == "provider/planner"


def test_feature_overlay_reports_field_level_provenance(tmp_path):
    plan = tmp_path / "plan"
    plan.mkdir()
    feature_path = plan / "feature.md"
    feature_path.write_text(
        "---\n"
        "slug: demo\n"
        "target_project: .\n"
        "requirement: requirement.md\n"
        "models:\n"
        "  worker:\n"
        "    model: feature/coder\n"
        "---\n"
    )
    global_path = tmp_path / "config.toml"
    global_path.write_text(
        '[models.worker]\nmodel = "global/coder"\nreasoning = "low"\n')
    resolution = resolve_config(repo=plan, global_path=global_path)
    view = inspect_preferences(resolution, feature_path=feature_path)
    worker = next(row for row in view.rows if row.role == "worker")
    assert worker.model == "feature/coder"
    assert worker.model_source == "feature"
    assert worker.reasoning == "low"
    assert worker.reasoning_source == "global"
    assert worker.model_path == feature_path.resolve()


def test_resolution_never_needs_dispatch_complete_roles(tmp_path):
    global_path = tmp_path / "config.toml"
    global_path.write_text('[models.worker]\nmodel = "only/worker"\n')
    feature = FeatureSpec(slug="demo", target_project=tmp_path,
                          requirement=tmp_path / "req.md",
                          models=RoleModels(planner=ModelSpec(model="p")))
    assert feature.models.worker is None
    view = inspect_preferences(resolve_config(global_path=global_path))
    planner = next(row for row in view.rows if row.role == "planner")
    assert planner.model is None


def test_feature_contract_error_never_echoes_input_values(tmp_path):
    sentinel = "https://hooks.invalid/feature-secret-token"
    feature_path = tmp_path / "feature.md"
    feature_path.write_text(
        "---\n"
        f"slug: {sentinel}\n"
        "target_project: .\n"
        "requirement: requirement.md\n"
        "---\n"
    )
    with pytest.raises(ContractError) as caught:
        inspect_preferences(resolve_config(None), feature_path=feature_path)
    message = str(caught.value)
    assert sentinel not in message
    assert "slug" in message
