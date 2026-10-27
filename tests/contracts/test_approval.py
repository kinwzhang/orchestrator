"""Approval hashing, configuration precedence and role model inheritance."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from orch.config import Config, effective_feature, load_config
from orch.contracts import (
    ContractError,
    FeatureSpec,
    ModelSpec,
    RoleModels,
    approval_hashes,
    detect_approval_changes,
    effective_settings,
)


def feature_for(plan: Path) -> FeatureSpec:
    return FeatureSpec(slug="demo", target_project=plan.parent, requirement=plan / "requirement.md")


def test_approval_covers_every_input_and_excludes_outputs(plan_factory):
    plan, manifest = plan_factory()
    approved = approval_hashes(plan, manifest, feature_for(plan))
    for name in ["feature.md", "requirement.md", "discussion.md", "plan.md", "manifest.md",
                 "reviewer.md", "doc-reviewer.md", "workers/w1.md"]:
        assert str((plan / name).resolve()) in approved, name
    assert {"effective_configuration", "effective_checks"} <= set(approved)
    (plan / "report.md").write_text("generated output\n")
    (plan / "run-history.txt").write_text("history\n")
    assert detect_approval_changes(approved, plan, manifest, feature_for(plan)) == []


def test_detect_reports_edited_missing_and_new_inputs(plan_factory):
    plan, manifest = plan_factory()
    approved = approval_hashes(plan, manifest, feature_for(plan))
    (plan / "workers/w1.md").write_text("steered instructions\n")
    changes = detect_approval_changes(approved, plan, manifest, feature_for(plan))
    assert any("workers/w1.md" in change and "content changed" in change for change in changes)
    (plan / "workers/w1.md").unlink()
    changes = detect_approval_changes(approved, plan, manifest, feature_for(plan))
    assert any("workers/w1.md" in change and "missing" in change for change in changes)
    assert detect_approval_changes({}, plan, manifest, feature_for(plan)) != []


def test_effective_settings_change_with_configuration(plan_factory, tmp_path):
    plan, manifest = plan_factory()
    source = plan / "feature.md"
    base = FeatureSpec(slug="demo", target_project=tmp_path, requirement=plan / "requirement.md")
    cheap = Config(models=RoleModels(worker=ModelSpec(model="global/coder")))
    strong = Config(models=RoleModels(worker=ModelSpec(model="other/coder")))
    first = effective_feature(base, cheap, source)
    second = effective_feature(base, strong, source)
    assert first.models.resolve("replacement").model == "global/coder"
    assert second.models.resolve("replacement").model == "other/coder"
    # A configuration edit cannot silently change an approved run: the
    # effective settings digest is part of the approval.
    assert approval_hashes(plan, manifest, first) != approval_hashes(plan, manifest, second)
    assert effective_settings(first, manifest) != effective_settings(second, manifest)


def test_approval_pins_runtime_policy_without_persisting_secrets(plan_factory, tmp_path):
    plan, manifest = plan_factory()
    feature = feature_for(plan)
    ordinary = effective_feature(feature, Config(), plan / "feature.md")
    secured_config = Config(viewer=True, webhook_env="ORCH_WEBHOOK",
                            redaction_env=["TOKEN_A", "TOKEN_B"])
    secured = effective_feature(feature, secured_config, plan / "feature.md")
    assert approval_hashes(plan, manifest, ordinary) != approval_hashes(plan, manifest, secured)
    assert "TOKEN_A" not in str(secured.model_dump())
    assert "ORCH_WEBHOOK" not in str(secured.model_dump())


def test_precedence_chain_global_repo_feature(tmp_path):
    global_file = tmp_path / "global.toml"
    global_file.write_text(
        '[models.worker]\nmodel = "global/coder"\nreasoning = "high"\n'
        '[limits]\nconcurrency = 2\nmax_runtime_min = 100\n'
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".orch.toml").write_text('[models.worker]\nmodel = "repo/coder"\n[limits]\nconcurrency = 3\n')
    config = load_config(repo, global_file)
    assert config.models.resolve("worker").model == "repo/coder"
    assert config.models.resolve("worker").reasoning == "high"
    assert config.limits.concurrency == 3
    assert config.limits.max_runtime_min == 100
    feature = FeatureSpec(slug="x", target_project=tmp_path, requirement=tmp_path / "req.md",
                          models=RoleModels(worker=ModelSpec(model="feature/coder")),
                          limits={"concurrency": 1})
    resolved = effective_feature(feature, config, tmp_path / "feature.md")
    assert resolved.models.resolve("worker").model == "feature/coder"
    assert resolved.models.resolve("worker").reasoning == "high"
    assert resolved.limits.concurrency == 1
    assert resolved.limits.max_runtime_min == 100


def test_replacement_inherits_worker_after_all_overrides(tmp_path):
    global_file = tmp_path / "global.toml"
    global_file.write_text('[models.worker]\nmodel = "global/coder"\n[models.planner]\nmodel = "global/planner"\n')
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".orch.toml").write_text('[models.worker]\nmodel = "repo/coder"\n')
    config = load_config(repo, global_file)
    feature = FeatureSpec(slug="x", target_project=tmp_path, requirement=tmp_path / "req.md",
                          models=RoleModels(worker=ModelSpec(model="feature/coder")))
    resolved = effective_feature(feature, config, tmp_path / "feature.md")
    assert resolved.models.resolve("replacement").model == "feature/coder"
    assert resolved.models.resolve("planner").model == "global/planner"
    with pytest.raises(ContractError, match="docs_reviewer"):
        resolved.models.resolve("docs_reviewer")


def test_replacement_cannot_declare_its_own_model(tmp_path):
    config_file = tmp_path / "global.toml"
    config_file.write_text('[models.replacement]\nmodel = "rogue/model"\n')
    with pytest.raises(ContractError, match="replacement"):
        load_config(None, config_file)
    with pytest.raises(ValidationError):
        RoleModels.model_validate({"replacement": {"model": "rogue/model"}})


def test_malformed_and_unknown_configuration_name_the_file(tmp_path):
    broken = tmp_path / "broken.toml"
    broken.write_text("[models\nworker = 1\n")
    with pytest.raises(ContractError, match="broken.toml"):
        load_config(None, broken)
    unknown = tmp_path / "unknown.toml"
    unknown.write_text("[nonsense]\nvalue = 1\n")
    with pytest.raises(ContractError, match="unknown.toml"):
        load_config(None, unknown)
    bad_limits = tmp_path / "limits.toml"
    bad_limits.write_text("[limits]\nconcurrency = 99\n")
    with pytest.raises(ContractError, match="limits.toml"):
        load_config(None, bad_limits)


def test_relative_data_dir_resolves_against_containing_file(tmp_path):
    config_file = tmp_path / "global.toml"
    config_file.write_text('data_dir = "state"\n')
    config = load_config(None, config_file)
    assert config.data_dir == (tmp_path / "state").resolve()


def test_feature_file_with_unknown_setting_names_the_file(tmp_path):
    from orch.contracts import read_markdown

    path = tmp_path / "feature.md"
    path.write_text("---\nslug: x\ntarget_project: .\nrequirement: r.md\nmax_rounds: 3\n---\n")
    with pytest.raises(ContractError, match="feature.md"):
        read_markdown(path, FeatureSpec)
