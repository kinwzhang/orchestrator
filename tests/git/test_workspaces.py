import json
import subprocess
from pathlib import Path

import pytest

from orch import git_ops
from orch.contracts import FeatureSpec, Manifest, WorkerSpec
from orch.git_ops import GitConflict, GitError, GitWorkspace, changed_paths, git, head


def command(path, *args):
    return git(path, *args).strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "project with spaces"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    command(repo, "config", "user.name", "Orch Test")
    command(repo, "config", "user.email", "orch@example.invalid")
    (repo / "old.txt").write_text("initial\n")
    (repo / "README.md").write_text("initial\n")
    command(repo, "add", ".")
    command(repo, "commit", "-m", "initial")
    return repo


def setup(repo, tmp_path, workers=None):
    manager = GitWorkspace(repo, tmp_path / "runs" / "test-run")
    feature = FeatureSpec(slug="demo", target_project=repo, requirement=repo / "requirement.md")
    workers = workers or [
        WorkerSpec(
            id="w01",
            title="one",
            instruction="workers/w01.md",
            allowed_paths=["one/**", "README.md", "old.txt"],
        )
    ]
    manifest = Manifest(slug="demo", branch="feature/demo", workers=workers)
    record = manager.prepare(feature, manifest)
    return manager, record


def test_prepare_is_idempotent_and_keeps_main_untouched(repo, tmp_path):
    original = head(repo)
    (repo / "uncommitted.txt").write_text("preserve")
    manager, record = setup(repo, tmp_path)
    again = GitWorkspace(repo, manager.run_dir).prepare(
        FeatureSpec(slug="demo", target_project=repo, requirement=repo / "requirement.md")
    )
    assert again.feature_path == record.feature_path
    assert command(repo, "branch", "--show-current") == "main"
    assert head(repo) == original
    assert (repo / "uncommitted.txt").read_text() == "preserve"
    assert record.workers["w01"].path.exists()


def test_prepare_recovers_original_base_after_feature_worktree_creation(repo, tmp_path, monkeypatch):
    initial = head(repo)
    run_dir = tmp_path / "runs" / "interrupted"
    manager = GitWorkspace(repo, run_dir)
    feature = FeatureSpec(slug="demo", target_project=repo, requirement=repo / "requirement.md")
    save = manager._save

    def stop_after_feature_creation():
        save()
        entry = manager.owned["resources"].get("feature")
        if entry and entry["status"] == "ready" and manager.record is None:
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(manager, "_save", stop_after_feature_creation)
    with pytest.raises(RuntimeError, match="interruption"):
        manager.prepare(feature)
    monkeypatch.setattr(manager, "_save", save)
    (repo / "README.md").write_text("advanced base\n")
    command(repo, "add", "README.md")
    command(repo, "commit", "-m", "advance base")

    recovered = GitWorkspace(repo, run_dir).prepare(feature)
    assert recovered.base_sha == initial
    assert head(recovered.feature_path) == initial


def test_prepare_recovers_worker_baseline_after_worktree_creation(repo, tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "interrupted-worker"
    manager = GitWorkspace(repo, run_dir)
    feature = FeatureSpec(slug="demo", target_project=repo, requirement=repo / "requirement.md")
    manifest = Manifest(
        slug="demo",
        branch="feature/demo",
        workers=[
            WorkerSpec(
                id="w01", title="one", instruction="workers/w01.md", allowed_paths=["one/**"]
            )
        ],
    )
    original_save = manager._save

    def stop_after_worker_creation():
        original_save()
        entry = manager.owned["resources"].get("worker-w01")
        if entry and entry["status"] == "ready" and "w01" not in manager.record.workers:
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(manager, "_save", stop_after_worker_creation)
    with pytest.raises(RuntimeError, match="interruption"):
        manager.prepare(feature, manifest)
    monkeypatch.setattr(manager, "_save", original_save)
    feature_path = run_dir / "worktrees" / "feature"
    (feature_path / "README.md").write_text("feature advanced\n")
    command(feature_path, "add", "README.md")
    command(feature_path, "commit", "-m", "advance feature")

    recovered = GitWorkspace(repo, run_dir).prepare(feature, manifest)
    assert recovered.workers["w01"].baseline_sha == recovered.base_sha
    assert head(recovered.workers["w01"].path) == recovered.base_sha


def test_existing_user_branch_is_not_adopted(repo, tmp_path):
    command(repo, "branch", "feature/demo")
    with pytest.raises(GitError, match="Refusing to adopt"):
        setup(repo, tmp_path)


def test_scope_handles_staged_untracked_deleted_and_rename(repo, tmp_path):
    manager, record = setup(repo, tmp_path)
    worker = record.workers["w01"]
    (worker.path / "old.txt").rename(worker.path / "renamed.txt")
    command(worker.path, "add", "--", "old.txt", "renamed.txt")
    (worker.path / "README.md").write_text("updated")
    (worker.path / "other.bin").write_bytes(b"\0\xff")
    (worker.path / ".plans").mkdir()
    (worker.path / ".plans" / "evil.md").write_text("forbidden")
    result = manager.inspect_changes(worker, allowed_paths=["README.md", "old.txt"])
    assert result.paths == [".plans/evil.md", "README.md", "old.txt", "other.bin", "renamed.txt"]
    assert result.violations == [".plans/evil.md", "other.bin", "renamed.txt"]


def test_scope_rejects_symlink_escape(repo, tmp_path):
    manager, record = setup(repo, tmp_path)
    worker = record.workers["w01"]
    external = tmp_path / "external"
    external.mkdir()
    try:
        (worker.path / "link").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not allow symlink creation")
    result = manager.inspect_changes(worker, allowed_paths=["link"])
    assert "link" in result.violations


def test_worker_commit_rejects_changed_scope_and_unexpected_head(repo, tmp_path):
    manager, record = setup(repo, tmp_path)
    worker = record.workers["w01"]
    (worker.path / "README.md").write_text("changed")
    result = manager.inspect_changes(worker, allowed_paths=["README.md"])
    (worker.path / "extra.txt").write_text("late")
    with pytest.raises(GitError, match="validated scope"):
        manager.commit_worker(worker, result)
    (worker.path / "extra.txt").unlink()
    sha = manager.commit_worker(worker, result)
    assert sha != worker.baseline_sha
    with pytest.raises(GitError, match="unexpectedly changed HEAD"):
        manager.commit_worker(worker, result)


def test_worker_commit_requires_scope_validated_changes(repo, tmp_path):
    manager, record = setup(repo, tmp_path)
    worker = record.workers["w01"]
    (worker.path / "README.md").write_text("changed")
    with pytest.raises(GitError, match="scope-validated"):
        manager.commit_worker(worker, manager.inspect_changes(worker))


def test_integrate_and_promote_only_feature(repo, tmp_path):
    manager, record = setup(repo, tmp_path)
    main = head(repo)
    worker = record.workers["w01"]
    (worker.path / "README.md").write_text("new docs")
    changes = manager.inspect_changes(worker, allowed_paths=["README.md"])
    sha = manager.commit_worker(worker, changes)
    expected = head(record.feature_path)
    candidate = manager.build_candidate({worker.id: sha}, expected)
    assert (candidate.path / "README.md").read_text() == "new docs"
    assert manager.promote_candidate(candidate, expected) == candidate.sha
    assert head(repo) == main
    assert head(record.feature_path) == candidate.sha
    # A crash after the fast-forward but before the workflow checkpoint is idempotent.
    assert manager.promote_candidate(candidate, expected) == candidate.sha
    # Candidate assembly can be replayed after a crash without repeating work.
    assert manager.build_candidate({worker.id: sha}, expected).sha == candidate.sha


def test_promotion_rejects_dirty_candidate_and_stale_feature(repo, tmp_path):
    manager, record = setup(repo, tmp_path)
    expected = head(record.feature_path)
    candidate = manager.build_candidate({}, expected)
    (candidate.path / "junk").write_text("x")
    with pytest.raises(GitError, match="candidate changed"):
        manager.promote_candidate(candidate, expected)
    (candidate.path / "junk").unlink()
    (record.feature_path / "metadata.md").write_text("manual")
    command(record.feature_path, "add", ".")
    command(record.feature_path, "commit", "-m", "manual feature update")
    with pytest.raises(GitError, match="HEAD changed"):
        manager.promote_candidate(candidate, expected)


def test_dependency_baseline_excludes_imported_changes(repo, tmp_path):
    specs = [
        WorkerSpec(id="w01", title="one", instruction="workers/w01.md", allowed_paths=["one/**"]),
        WorkerSpec(
            id="w02",
            title="two",
            instruction="workers/w02.md",
            allowed_paths=["two/**"],
            depends_on=["w01"],
        ),
    ]
    manager, record = setup(repo, tmp_path, specs)
    one, two = record.workers.values()
    (one.path / "one").mkdir()
    (one.path / "one" / "api.txt").write_text("interface")
    sha = manager.commit_worker(one, manager.inspect_changes(one, allowed_paths=["one/**"]))
    manager.import_dependencies(two, [sha])
    assert (two.path / "one" / "api.txt").read_text() == "interface"
    assert manager.inspect_changes(two, allowed_paths=["two/**"]).paths == []
    (two.path / "two").mkdir()
    (two.path / "two" / "consumer.txt").write_text("consumer")
    two_sha = manager.commit_worker(two, manager.inspect_changes(two, allowed_paths=["two/**"]))
    candidate = manager.build_candidate({"w01": sha, "w02": two_sha}, head(record.feature_path))
    assert (candidate.path / "one" / "api.txt").exists()
    assert (candidate.path / "two" / "consumer.txt").exists()


def test_conflict_preserved_for_resolver(repo, tmp_path):
    manager, record = setup(
        repo,
        tmp_path,
        [
            WorkerSpec(
                id="w01", title="one", instruction="workers/w01.md", allowed_paths=["one/**"]
            ),
            WorkerSpec(
                id="w02", title="two", instruction="workers/w02.md", allowed_paths=["two/**"]
            ),
        ],
    )
    shas = {}
    # Deliberately bypass scope rules here to exercise an unexpected integration conflict.
    for worker in record.workers.values():
        (worker.path / "old.txt").write_text(worker.id)
        shas[worker.id] = manager.commit_worker(
            worker, manager.inspect_changes(worker, allowed_paths=["old.txt"])
        )
    with pytest.raises(GitConflict) as error:
        manager.build_candidate(shas, head(record.feature_path))
    assert error.value.record.paths == ["old.txt"]
    assert error.value.record.operation == "merge"
    assert (manager.run_dir / "conflict.json").exists()
    assert head(repo) == record.base_sha


def test_clean_preserves_dirty_work_and_user_resources(repo, tmp_path):
    manager, record = setup(repo, tmp_path)
    worker = record.workers["w01"]
    (worker.path / "partial.txt").write_text("unfinished")
    with pytest.raises(GitError, match="dirty"):
        manager.clean(record)
    assert (worker.path / "partial.txt").read_text() == "unfinished"
    assert record.feature_path.exists()
    assert repo.exists()


def test_clean_retains_feature_and_main(repo, tmp_path):
    manager, record = setup(repo, tmp_path)
    manager.clean(record)
    assert record.feature_path.exists()
    assert not record.workers["w01"].path.exists()
    assert command(repo, "branch", "--show-current") == "main"


def test_changed_paths_includes_committed_diff(repo, tmp_path):
    manager, record = setup(repo, tmp_path)
    worker = record.workers["w01"]
    (worker.path / "README.md").write_text("change")
    manager.commit_worker(worker, manager.inspect_changes(worker, allowed_paths=["README.md"]))
    assert changed_paths(worker.path, worker.baseline_sha) == ["README.md"]


def test_late_same_path_mutation_invalidates_checks(repo, tmp_path):
    manager, record = setup(repo, tmp_path)
    worker = record.workers["w01"]
    (worker.path / "README.md").write_text("checked content")
    changes = manager.inspect_changes(worker, allowed_paths=["README.md"])
    (worker.path / "README.md").write_text("unchecked content")
    with pytest.raises(GitError, match="validated scope"):
        manager.commit_worker(worker, changes)


def test_worker_commit_retains_reconciliation_evidence_after_restart(repo, tmp_path):
    manager, record = setup(repo, tmp_path)
    worker = record.workers["w01"]
    baseline = worker.baseline_sha
    (worker.path / "README.md").write_text("durable commit\n")
    changes = manager.inspect_changes(worker, allowed_paths=["README.md"])
    committed = manager.commit_worker(worker, changes)

    recovered = GitWorkspace(repo, manager.run_dir)
    recovered_worker = recovered.record.workers[worker.id]
    assert head(recovered_worker.path) == committed
    assert git(recovered_worker.path, "rev-parse", "HEAD^").strip() == baseline
    actual = recovered.inspect_changes(
        recovered_worker,
        baseline_sha=baseline,
        allowed_paths=["README.md"],
    )
    assert actual.violations == []
    assert actual.fingerprint == changes.fingerprint


def test_clean_preserves_clean_but_unmerged_worktree(repo, tmp_path):
    manager, record = setup(repo, tmp_path)
    worker = record.workers["w01"]
    (worker.path / "README.md").write_text("unfinished delivery")
    manager.commit_worker(worker, manager.inspect_changes(worker, allowed_paths=["README.md"]))
    with pytest.raises(GitError, match="unmerged"):
        manager.clean(record)
    assert worker.path.exists()
    assert (worker.path / "README.md").read_text() == "unfinished delivery"


def conflict_setup(repo, tmp_path):
    manager, record = setup(
        repo,
        tmp_path,
        [
            WorkerSpec(
                id="w01", title="one", instruction="workers/w01.md", allowed_paths=["one/**"]
            ),
            WorkerSpec(
                id="w02", title="two", instruction="workers/w02.md", allowed_paths=["two/**"]
            ),
        ],
    )
    shas = {}
    for worker in record.workers.values():
        (worker.path / "old.txt").write_text(worker.id)
        shas[worker.id] = manager.commit_worker(
            worker, manager.inspect_changes(worker, allowed_paths=["old.txt"])
        )
    with pytest.raises(GitConflict) as error:
        manager.build_candidate(shas, head(record.feature_path))
    return manager, record, shas, error.value.record


def test_resolver_cherry_pick_coverage_continues_candidate(repo, tmp_path):
    manager, record, shas, conflict = conflict_setup(repo, tmp_path)
    path = Path(conflict.path)
    command(path, "merge", "--abort")
    subprocess.run(
        ["git", "-C", str(path), "cherry-pick", *conflict.source_commits],
        check=False,
        capture_output=True,
    )
    (path / "old.txt").write_text("combined intended result")
    command(path, "add", "old.txt")
    command(path, "cherry-pick", "--continue")
    resolved = manager.accept_resolution(conflict, conflict.source_commits, ["old.txt"])
    candidate = manager.build_candidate(shas, head(record.feature_path))
    assert candidate.sha == resolved
    assert (candidate.path / "old.txt").read_text() == "combined intended result"
    assert "covered_sources" in (manager.run_dir / "integration-1-resolutions.json").read_text()


def test_resolver_must_cover_sources_and_stay_in_scope(repo, tmp_path):
    manager, record, _shas, conflict = conflict_setup(repo, tmp_path)
    path = Path(conflict.path)
    (path / "old.txt").write_text("resolved")
    (path / "unrelated.txt").write_text("not permitted")
    command(path, "add", ".")
    command(path, "commit", "-m", "resolver commit")
    with pytest.raises(GitError, match="every conflicting"):
        manager.accept_resolution(conflict, [])
    with pytest.raises(GitError, match="unrelated"):
        manager.accept_resolution(conflict, conflict.source_commits)
    assert path.exists()
    assert head(record.feature_path) == record.base_sha


def test_dependency_resolver_updates_baseline(repo, tmp_path):
    manager, record, shas, conflict = conflict_setup(repo, tmp_path)
    worker = record.workers["w02"]
    with pytest.raises(GitConflict) as error:
        manager.import_dependencies(worker, [shas["w01"]])
    conflict = error.value.record
    (worker.path / "old.txt").write_text("dependency resolved")
    command(worker.path, "add", "old.txt")
    command(worker.path, "commit", "-m", "resolve dependency")
    result = manager.accept_resolution(conflict, conflict.source_commits, ["old.txt"])
    assert manager.record.workers[worker.id].baseline_sha == result
    recovered = GitWorkspace(repo, manager.run_dir)
    assert recovered.record.workers[worker.id].baseline_sha == result


def test_candidate_reconciles_crash_after_merge_before_progress_save(repo, tmp_path, monkeypatch):
    manager, record = setup(repo, tmp_path)
    worker = record.workers["w01"]
    (worker.path / "README.md").write_text("candidate content\n")
    source = manager.commit_worker(
        worker, manager.inspect_changes(worker, allowed_paths=["README.md"])
    )
    base = head(record.feature_path)
    atomic_write = git_ops.atomic_write

    def fail_after_merge(path, text):
        if Path(path).name == "integration-1.json" and json.loads(text).get("merged"):
            raise RuntimeError("simulated crash after merge")
        atomic_write(path, text)

    monkeypatch.setattr(git_ops, "atomic_write", fail_after_merge)
    with pytest.raises(RuntimeError, match="after merge"):
        manager.build_candidate({worker.id: source}, base)
    monkeypatch.setattr(git_ops, "atomic_write", atomic_write)

    recovered = GitWorkspace(repo, manager.run_dir)
    candidate = recovered.build_candidate({worker.id: source}, base)
    assert candidate.source_shas == [source]
    assert (candidate.path / "README.md").read_text() == "candidate content\n"
    assert recovered.promote_candidate(candidate, base) == candidate.sha


def test_candidate_orders_dependency_sources_before_dependent_ids(repo, tmp_path):
    manager, record = setup(
        repo,
        tmp_path,
        [
            WorkerSpec(
                id="a-consumer",
                title="consumer",
                instruction="workers/a.md",
                allowed_paths=["consumer/**"],
                depends_on=["z-producer"],
            ),
            WorkerSpec(
                id="z-producer",
                title="producer",
                instruction="workers/z.md",
                allowed_paths=["producer/**"],
            ),
        ],
    )
    consumer = record.workers["a-consumer"]
    producer = record.workers["z-producer"]
    (producer.path / "producer").mkdir()
    (producer.path / "producer" / "api.txt").write_text("api")
    producer_sha = manager.commit_worker(
        producer, manager.inspect_changes(producer, allowed_paths=["producer/**"])
    )
    manager.import_dependencies(consumer, [producer_sha])
    (consumer.path / "consumer").mkdir()
    (consumer.path / "consumer" / "use.txt").write_text("uses api")
    consumer_sha = manager.commit_worker(
        consumer, manager.inspect_changes(consumer, allowed_paths=["consumer/**"])
    )

    candidate = manager.build_candidate(
        {"a-consumer": consumer_sha, "z-producer": producer_sha},
        head(record.feature_path),
    )
    progress = json.loads((manager.run_dir / "integration-1.json").read_text())
    assert progress["sources"] == [producer_sha, consumer_sha]
    assert (candidate.path / "producer" / "api.txt").exists()
    assert (candidate.path / "consumer" / "use.txt").exists()
