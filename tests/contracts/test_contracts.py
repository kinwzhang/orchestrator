from pathlib import Path

import pytest
from pydantic import ValidationError

from orch.config import Config, effective_feature, load_config
from orch.contracts import (
    ContractError,
    Discussion,
    FeatureSpec,
    Manifest,
    RoleModels,
    RunState,
    Verdict,
    WorkerSpec,
    approved_files,
    file_hashes,
    matches,
    read_markdown,
    within,
    write_markdown,
)
from orch.store import StateStore


def worker(name="one", path="src/one/**", dependencies=None):
    return WorkerSpec(id=name, title=name, instruction=f"workers/{name}.md",
                      allowed_paths=[path], depends_on=dependencies or [])


def test_markdown_strict_roundtrip(tmp_path):
    path = tmp_path / "discussion.md"
    write_markdown(path, Discussion(ready=True), "## Decisions\nComplete\n")
    value, body = read_markdown(path, Discussion)
    assert value.ready and "Complete" in body
    for text in ["plain", "---\nready: true\nready: false\n---\n", "---\nready: true\nunknown: 1\n---\n", "---\nready: !!python/object {}\n---\n"]:
        path.write_text(text)
        with pytest.raises(ContractError):
            read_markdown(path, Discussion)


@pytest.mark.parametrize("value", ["../escape", "/absolute", "C:/drive", "a\\b", "a/../b"])
def test_unsafe_paths(tmp_path, value):
    with pytest.raises(ValueError):
        within(tmp_path, value)


def test_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink privilege unavailable")
    with pytest.raises(ContractError):
        within(root, "link/file")


def test_globs_and_scopes():
    assert matches("src/one.py", "src/**")
    assert matches("src/one.py", "src/**/*.py")
    assert not matches("other/src/one.py", "src/**")
    assert not matches("src/deep/one.py", "src/*.py")
    for pattern in ["**", ".plans/**", ".plans/feature.md"]:
        with pytest.raises(ValidationError):
            worker(path=pattern)
    with pytest.raises(ValidationError, match="Overlapping"):
        Manifest(slug="x", branch="feature/x", workers=[worker(), worker("two", "SRC/one/file.py")])


def test_dag_validation():
    Manifest(slug="x", branch="feature/x", workers=[worker(), worker("two", "src/two/**", ["one"])])
    with pytest.raises(ValidationError, match="cycle"):
        Manifest(slug="x", branch="feature/x", workers=[worker(dependencies=["two"]), worker("two", "src/two/**", ["one"])])
    with pytest.raises(ValidationError, match="Unknown"):
        Manifest(slug="x", branch="feature/x", workers=[worker(dependencies=["unknown"])])


def test_verdict_consistency():
    with pytest.raises(ValidationError):
        Verdict(reviewer="code", result="rework")
    Verdict(reviewer="docs", result="pass")


def test_config_precedence_and_replacement(tmp_path):
    global_file = tmp_path / "global.toml"
    global_file.write_text('[models.worker]\nmodel="global/coder"\n[limits]\nconcurrency=2\n')
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".orch.toml").write_text('[models.worker]\nmodel="repo/coder"\n')
    config = load_config(repo, global_file)
    assert config.models.resolve("replacement").model == "repo/coder"
    feature = FeatureSpec(slug="x", target_project=Path("target"), requirement=Path("req.md"),
                          models=RoleModels(worker={"model": "feature/coder"}))
    resolved = effective_feature(feature, config, tmp_path / "feature.md")
    assert resolved.models.resolve("replacement").model == "feature/coder"
    assert resolved.limits.concurrency == 2
    assert resolved.target_project == tmp_path / "target"
    with pytest.raises(ContractError):
        config.models.resolve("planner")


def test_approval_hashes_exclude_outputs(tmp_path):
    manifest = Manifest(slug="x", branch="feature/x", workers=[worker()])
    paths = approved_files(tmp_path, manifest)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name)
    hashes = file_hashes(paths)
    (tmp_path / "report.md").write_text("generated")
    assert file_hashes(approved_files(tmp_path, manifest)) == hashes
    (tmp_path / "workers/one.md").write_text("steered")
    assert file_hashes(paths) != hashes


def test_durable_state_revision_and_markdown_reconciliation(tmp_path):
    store = StateStore(tmp_path)
    state = store.save(RunState(run_id="run-1", slug="x"))
    stale = state.model_copy(deep=True)
    state.workers["one"] = {"status": "running", "attempt": 1}
    state = store.save(state)
    with pytest.raises(ValueError, match="Stale"):
        store.save(stale)
    event = store.event(state.run_id, "dispatch_intent", agent="one")
    assert event["id"] == 1
    log = store.run_dir(state.run_id) / "activity_log.md"
    log.unlink()
    store.render(store.load(state.run_id))
    assert "dispatch_intent" in log.read_text()
    assert "one" in (store.run_dir(state.run_id) / "checklist.md").read_text()
    assert len(store.events(state.run_id)) == 1


def test_global_lock_excludes_second_runner(tmp_path):
    import portalocker
    first, second = StateStore(tmp_path), StateStore(tmp_path)
    with first.lock(), pytest.raises(portalocker.exceptions.LockException), second.lock():
        pass


def test_check_names_cannot_escape_run_directory():
    from orch.contracts import CheckSpec
    with pytest.raises(ValidationError):
        CheckSpec(name="../../escape", argv=["python", "-m", "pytest"])
    with pytest.raises(ValidationError):
        CheckSpec(name="test", cmd="pytest")


def test_template_contracts_validate():
    from orch.contracts import Handover
    root = Path(__file__).parents[2] / "src/orch/templates"
    for name, schema in [("feature", FeatureSpec), ("manifest", Manifest),
                         ("discussion", Discussion), ("handover", Handover), ("verdict", Verdict)]:
        read_markdown(root / f"{name}.md", schema)


def test_ready_cannot_be_truthy_string(tmp_path):
    path = tmp_path / "discussion.md"
    path.write_text('---\nready: "false"\nblocking_questions: []\n---\n')
    with pytest.raises(ContractError):
        read_markdown(path, Discussion)


def test_feature_template_roundtrip_preserves_global_inheritance(tmp_path):
    feature = FeatureSpec(slug="x", target_project=tmp_path, requirement=tmp_path / "req.md")
    path = tmp_path / "feature.md"
    write_markdown(path, feature)
    read, _ = read_markdown(path, FeatureSpec)
    config = Config(models=RoleModels(worker={"model": "global/coder"}), limits={"concurrency": 2})
    effective = effective_feature(read, config, path)
    assert effective.models.resolve("worker").model == "global/coder"
    assert effective.limits.concurrency == 2


def test_worker_scope_case_collisions_and_duplicates_rejected():
    from orch.contracts import WorkerSpec
    with pytest.raises(ValidationError, match="case-colliding"):
        WorkerSpec(id="w", title="t", instruction="workers/w.md",
                   allowed_paths=["src/Module.py", "src/module.py"])
    with pytest.raises(ValidationError, match="Duplicate"):
        WorkerSpec(id="w", title="t", instruction="workers/w.md",
                   allowed_paths=["src/a.py", "src/a.py"])
    # Distinct portable scopes remain accepted.
    WorkerSpec(id="w", title="t", instruction="workers/w.md",
               allowed_paths=["src/orch/**", "src/orch/config.py"])


def test_check_shell_policy_is_explicit():
    from orch.contracts import CheckSpec
    CheckSpec(name="tests", argv=["python", "-m", "pytest", "-q"])
    CheckSpec(name="lint", cmd="ruff check .", shell="bash")
    with pytest.raises(ValidationError, match="explicit shell"):
        CheckSpec(name="lint", cmd="ruff check .")
    with pytest.raises(ValidationError, match="only to cmd"):
        CheckSpec(name="tests", argv=["python", "-m", "pytest"], shell="pwsh")
    with pytest.raises(ValidationError):
        CheckSpec(name="both", argv=["python"], cmd="python")


def test_manifest_rejects_protected_or_identical_branches():
    with pytest.raises(ValidationError, match="never be main"):
        Manifest(slug="x", branch="main", workers=[worker()])
    with pytest.raises(ValidationError, match="differ from the base"):
        Manifest(slug="x", branch="feature/x", base_branch="feature/x", workers=[worker()])
    Manifest(slug="x", branch="feature/x", workers=[worker()])
    with pytest.raises(ValidationError, match="feature/<slug>"):
        Manifest(slug="x", branch="feature/other", workers=[worker()])


def test_manifest_rejects_duplicate_checks_dependencies_and_reviewer_paths():
    from orch.contracts import CheckSpec

    with pytest.raises(ValidationError, match="Duplicate check"):
        Manifest(slug="x", branch="feature/x", workers=[worker()], checks=[
            CheckSpec(name="tests", argv=["pytest"]), CheckSpec(name="TESTS", argv=["pytest"])
        ])
    with pytest.raises(ValidationError, match="Duplicate worker dependency"):
        Manifest(slug="x", branch="feature/x", workers=[
            worker(), worker("two", "src/two/**", ["one", "one"])
        ])
    with pytest.raises(ValidationError, match="reviewer instructions must differ"):
        Manifest(slug="x", branch="feature/x", workers=[worker()],
                 reviewers={"code": "review.md", "docs": "REVIEW.md"})


def test_handover_body_section_and_shape_enforced(tmp_path):
    from orch.contracts import Handover
    path = tmp_path / "handover.md"
    value = Handover(agent="w1", round=1, status="done", files_changed=["a.py"],
                     docs_changed=["a.py"], checks_run=[])
    write_markdown(path, value, "# Handover\n\n## Work completed\n\nDone.\n")
    with pytest.raises(ContractError, match="Interfaces and contracts"):
        read_markdown(path, Handover)
    write_markdown(path, value, "# Handover\n\n## Interfaces and contracts\n\nStable API.\n")
    parsed, body = read_markdown(path, Handover)
    assert parsed.agent == "w1" and "Stable API" in body
    with pytest.raises(ValidationError):
        Handover(agent="bad id", round=1, status="done", files_changed=[],
                 docs_changed=[], checks_run=[])
    with pytest.raises(ValidationError, match="subset"):
        Handover(agent="w1", round=1, status="done", files_changed=["a.py"],
                 docs_changed=["b.py"], checks_run=[])
    with pytest.raises(ValidationError, match="evidence"):
        from orch.contracts import CheckEvidence

        CheckEvidence(name="tests", outcome="pass", evidence="../outside.log")
    with pytest.raises(ValidationError, match=r"\.plans"):
        Handover(agent="w1", round=1, status="done", files_changed=[".plans/x/plan.md"],
                 docs_changed=[], checks_run=[])


def test_finding_lineage_sanity_at_schema_level():
    from orch.contracts import Finding
    Finding(id="f1", worker="w1", severity="major", action="rework_same_worker",
            summary="Fix", root_issue_id="f1")
    Finding(id="f1-1", worker="w1", severity="minor", action="rework_same_worker",
            summary="Derived", parent_issue_id="f1", root_issue_id="f1")
    with pytest.raises(ValidationError, match="own parent"):
        Finding(id="f1", worker="w1", severity="major", action="rework_same_worker",
                summary="Fix", parent_issue_id="f1", root_issue_id="f1")
    with pytest.raises(ValidationError, match="root issue cannot declare a parent"):
        Finding(id="f1", worker="w1", severity="major", action="rework_same_worker",
                summary="Fix", parent_issue_id="other", root_issue_id="f1")
    with pytest.raises(ValidationError):
        Finding(id="", worker="w1", severity="major", action="rework_same_worker",
                summary="Fix", root_issue_id="f1")


def test_issue_state_tracks_attempt_evidence_and_reassignments():
    from orch.contracts import AssignmentState, IssueState
    issue = IssueState(id="f1", worker="w1", root_issue_id="f1",
                       evidence=["round-1/verdict-code.md"])
    assert issue.reassignments == 0 and issue.attempt == 1
    issue.assignments.append(AssignmentState())
    assert issue.reassignments == 1
    reloaded = IssueState.model_validate_json(issue.model_dump_json())
    assert reloaded.evidence == ["round-1/verdict-code.md"]


def test_run_state_rejects_unsupported_versions():
    with pytest.raises(ValidationError):
        RunState(run_id="r", slug="x", version=2)
    assert RunState(run_id="r", slug="x").updated_at is None


def test_globs_include_zero_directory_components():
    assert matches("src/b.py", "src/**/b.py")
    assert matches("src/a/b.py", "src/**/b.py")
    assert not matches("other/b.py", "src/**/b.py")
    assert matches("src/a/b/c.py", "src/**/*.py")


@pytest.mark.parametrize("value", ["a//b", "a/./b", "a/../b", "a/", "./a"])
def test_safe_relative_rejects_empty_and_relative_components(value):
    from orch.contracts import safe_relative
    with pytest.raises(ValueError):
        safe_relative(value)
