"""Git workspaces owned by one run; no operation promotes changes beyond its feature."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from orch.contracts import FeatureSpec, Manifest, matches, safe_relative, within
from orch.store import atomic_write


class GitError(RuntimeError):
    """Git infrastructure or workspace validation failed."""


@dataclass
class ConflictRecord:
    path: str
    operation: str
    target_head: str
    source_commits: list[str]
    paths: list[str]


class GitConflict(GitError):
    def __init__(self, record: ConflictRecord):
        self.record = record
        super().__init__(f"{record.operation} conflict in {record.path}: {', '.join(record.paths)}")


@dataclass
class WorkerWorkspace:
    id: str
    path: Path
    branch: str
    baseline_sha: str

    def to_dict(self) -> dict:
        return {**asdict(self), "path": str(self.path)}

    @classmethod
    def from_dict(cls, value: dict):
        return cls(**{**value, "path": Path(value["path"])})


@dataclass
class WorkspaceRecord:
    repo: Path
    feature_path: Path
    feature_branch: str
    base_sha: str
    workers: dict[str, WorkerWorkspace] = field(default_factory=dict)

    @property
    def plan_path(self) -> Path:
        return self.feature_path / ".plans" / self.feature_branch.removeprefix("feature/")

    def to_dict(self) -> dict:
        return {
            "repo": str(self.repo),
            "feature_path": str(self.feature_path),
            "feature_branch": self.feature_branch,
            "base_sha": self.base_sha,
            "workers": {k: v.to_dict() for k, v in self.workers.items()},
        }

    @classmethod
    def from_dict(cls, value: dict):
        return cls(
            repo=Path(value["repo"]),
            feature_path=Path(value["feature_path"]),
            feature_branch=value["feature_branch"],
            base_sha=value["base_sha"],
            workers={k: WorkerWorkspace.from_dict(v) for k, v in value.get("workers", {}).items()},
        )


@dataclass
class ChangeSet:
    paths: list[str]
    violations: list[str] = field(default_factory=list)
    fingerprint: str | None = None
    allowed_paths: list[str] | None = None

    @property
    def valid(self) -> bool:
        return not self.violations


@dataclass
class Candidate:
    sha: str
    path: Path
    source_shas: list[str]
    branch: str

    def to_dict(self) -> dict:
        return {**asdict(self), "path": str(self.path)}

    @classmethod
    def from_dict(cls, value: dict):
        return cls(**{**value, "path": Path(value["path"])})


def git(path: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *map(str, args)],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    if check and result.returncode:
        raise GitError(f"git {' '.join(args[:2])}: {result.stderr.strip()}")
    return result.stdout


def head(path: Path) -> str:
    return git(path, "rev-parse", "HEAD").strip()


def changed_paths(path: Path, baseline_sha: str) -> list[str]:
    # --no-renames exposes both ends of a rename, including its deleted source.
    tracked = git(path, "diff", "--name-only", "--no-renames", "-z", baseline_sha, "--")
    untracked = git(path, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted({x for x in (tracked + untracked).split("\0") if x})


class GitWorkspace:
    """A run's ownership journal is written before creating branches or worktrees."""

    def __init__(self, repo: Path, run_dir: Path):
        self.repo = Path(git(Path(repo), "rev-parse", "--show-toplevel").strip()).resolve()
        self.common_dir = Path(
            git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        ).resolve()
        self.run_dir = Path(run_dir).resolve()
        worktree_roots = [
            Path(line.removeprefix("worktree ")).resolve()
            for line in git(self.repo, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")
        ]
        if any(self.run_dir.is_relative_to(root) for root in worktree_roots):
            raise GitError("Run directory must be outside every target worktree")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.ownership_path = self.run_dir / "git_ownership.json"
        self.record_path = self.run_dir / "workspace.json"
        if self.ownership_path.exists():
            self.owned = json.loads(self.ownership_path.read_text())
            if self.owned["common_dir"] != str(self.common_dir):
                raise GitError("Run belongs to a different git repository")
        else:
            self.owned = {"common_dir": str(self.common_dir), "resources": {}}
        self.record = (
            WorkspaceRecord.from_dict(json.loads(self.record_path.read_text()))
            if self.record_path.exists()
            else None
        )

    def _save(self) -> None:
        atomic_write(self.ownership_path, json.dumps(self.owned, indent=2))
        if self.record:
            atomic_write(self.record_path, json.dumps(self.record.to_dict(), indent=2))

    def _sha(self, ref: str) -> str:
        if ref.startswith("-") or any(c in ref for c in "\n\r\0"):
            raise GitError("Invalid git ref")
        return git(self.repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()

    def _registered_worktrees(self) -> dict[Path, str]:
        result = {}
        for block in git(self.repo, "worktree", "list", "--porcelain").split("\n\n"):
            lines = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
            if "worktree" in lines:
                result[Path(lines["worktree"]).resolve()] = lines.get("branch", "")
        return result

    def _owned_worktree(self, name: str, branch: str, start: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise GitError("Invalid workspace name")
        if not branch.startswith(("feature/", "orch/")):
            raise GitError("Only feature/ and orch/ branches may be created")
        git(self.repo, "check-ref-format", "--branch", branch)
        path = self.run_dir / "worktrees" / name
        resources = self.owned["resources"]
        entry = resources.get(name)
        registered = self._registered_worktrees()
        if not entry:
            if path.exists() or git(
                self.repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False
            ):
                raise GitError(f"Refusing to adopt existing branch/worktree: {branch}")
            # Intent persists before side effects, allowing crash reconciliation.
            entry = {"path": str(path), "branch": branch, "start": start, "status": "creating"}
            resources[name] = entry
            self._save()
        if entry["path"] != str(path) or entry["branch"] != branch:
            raise GitError("Owned worktree identity changed")
        if path in registered:
            if registered[path] != f"refs/heads/{branch}":
                raise GitError("Owned worktree branch was changed")
            entry["status"] = "ready"
            self._save()
            return path
        if path.exists():
            raise GitError("Owned path exists but is not a registered worktree")
        exists = bool(git(self.repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False))
        if exists:
            # A crash after branch creation but before worktree registration is recoverable
            # only if the ref still has the recorded creation value.
            if self._sha(branch) != entry["start"] or entry["status"] != "creating":
                raise GitError("Cannot reconcile existing owned branch")
            git(self.repo, "worktree", "add", str(path), branch)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            git(self.repo, "worktree", "add", "-b", branch, str(path), entry["start"])
        entry["status"] = "ready"
        self._save()
        return path

    def prepare(self, feature: FeatureSpec, manifest: Manifest | None = None) -> WorkspaceRecord:
        branch = f"feature/{feature.slug}"
        if manifest and manifest.branch != branch:
            raise GitError(f"Feature branch must be {branch}")
        if self.record:
            if self.record.feature_branch != branch:
                raise GitError("Run belongs to a different feature")
            self._validate_record(self.record)
        else:
            base_sha = self._sha(feature.base_branch)
            path = self._owned_worktree("feature", branch, base_sha)
            # The ownership intent is written before `worktree add`. If the process
            # stops after creating the checkout but before saving workspace.json,
            # reuse the original start point rather than a possibly advanced base ref.
            start_sha = self._sha(self.owned["resources"]["feature"]["start"])
            self.record = WorkspaceRecord(self.repo, path, branch, start_sha)
            self._save()
        if manifest:
            self.prepare_workers(self.record, manifest)
        return self.record

    def _validate_record(self, record: WorkspaceRecord) -> None:
        if record.repo.resolve() != self.repo:
            raise GitError("Workspace repository changed")
        if not record.feature_branch.startswith("feature/"):
            raise GitError("Feature branch is not a permitted destination")
        registered = self._registered_worktrees()
        if registered.get(record.feature_path.resolve()) != f"refs/heads/{record.feature_branch}":
            raise GitError("Feature checkout is missing or switched")
        if record.feature_path.resolve() != self.run_dir / "worktrees" / "feature":
            raise GitError("Feature checkout is outside owned paths")

    def prepare_workers(self, record: WorkspaceRecord, manifest: Manifest) -> WorkspaceRecord:
        self._validate_record(record)
        if manifest.branch != record.feature_branch:
            raise GitError("Manifest cannot change the feature merge destination")
        for spec in manifest.workers:
            branch = f"orch/{manifest.slug}/{self.run_dir.name}/{spec.id}"
            if spec.id not in record.workers:
                baseline = head(record.feature_path)
                path = self._owned_worktree(f"worker-{spec.id}", branch, baseline)
                start_sha = self._sha(self.owned["resources"][f"worker-{spec.id}"]["start"])
                record.workers[spec.id] = WorkerWorkspace(spec.id, path, branch, start_sha)
                self.record = record
                self._save()
            else:
                self._validate_worker(record.workers[spec.id])
        return record

    def _validate_worker(self, worker: WorkerWorkspace) -> None:
        entry = self.owned["resources"].get(f"worker-{worker.id}")
        if (
            not entry
            or Path(entry["path"]).resolve() != worker.path.resolve()
            or entry["branch"] != worker.branch
        ):
            raise GitError("Worker is not owned by this run")
        registered = self._registered_worktrees()
        if registered.get(worker.path.resolve()) != f"refs/heads/{worker.branch}":
            raise GitError("Worker branch changed or worktree is missing")

    def inspect_changes(
        self,
        worker: WorkerWorkspace,
        baseline_sha: str | None = None,
        allowed_paths: list[str] | None = None,
    ) -> ChangeSet:
        self._validate_worker(worker)
        baseline = baseline_sha or worker.baseline_sha
        paths = changed_paths(worker.path, self._sha(baseline))
        violations = []
        folded = {}
        tracked = git(worker.path, "ls-files", "-z").split("\0")
        for path in set(tracked + paths) - {""}:
            key = path.casefold()
            if key in folded and folded[key] != path:
                violations.extend([path, folded[key]])
            folded[key] = path
        for path in paths:
            try:
                safe_relative(path)
                within(worker.path, path)
            except ValueError:
                violations.append(path)
                continue
            if (
                path.casefold() == ".plans"
                or path.casefold().startswith(".plans/")
                or allowed_paths is not None
                and not any(matches(path, pat) for pat in allowed_paths)
            ):
                violations.append(path)
        digest = hashlib.sha256()
        digest.update(
            git(worker.path, "diff", "--binary", "--no-ext-diff", baseline, "--").encode(
                "utf-8", errors="surrogateescape"
            )
        )
        for path in paths:
            full = worker.path / path
            digest.update(path.encode("utf-8", errors="surrogateescape"))
            if full.is_symlink():
                digest.update(str(full.readlink()).encode("utf-8", errors="surrogateescape"))
            elif full.is_file():
                digest.update(full.read_bytes())
            else:
                digest.update(b"<missing>")
        return ChangeSet(paths, sorted(set(violations)), digest.hexdigest(), allowed_paths)

    def commit_worker(
        self, worker: WorkerWorkspace, validated_changes: ChangeSet
    ) -> str:
        self._validate_worker(worker)
        if validated_changes.allowed_paths is None:
            raise GitError("Worker commit requires a scope-validated change set")
        if head(worker.path) != worker.baseline_sha:
            raise GitError("Worker unexpectedly changed HEAD; reconcile before committing")
        actual = self.inspect_changes(worker, allowed_paths=validated_changes.allowed_paths)
        if (
            validated_changes.violations
            or actual.paths != validated_changes.paths
            or (
                validated_changes.fingerprint is not None
                and actual.fingerprint != validated_changes.fingerprint
            )
        ):
            raise GitError("Worker changes differ from the validated scope")
        if actual.violations:
            raise GitError("Forbidden worker changes")
        if actual.paths:
            git(worker.path, "add", "--", *actual.paths)
            git(worker.path, "commit", "-m", f"orch: {worker.id} implementation")
        sha = head(worker.path)
        return sha

    def commit_plan(self, plan_path: Path, message: str = "orch: approved plan") -> str:
        if not self.record:
            raise GitError("Prepare a feature checkout first")
        self._validate_record(self.record)
        relative = plan_path.resolve().relative_to(self.record.feature_path.resolve()).as_posix()
        if not relative.startswith(".plans/"):
            raise GitError("Plan commit is restricted to .plans")
        # Never accidentally include someone else's pre-staged changes.
        staged = git(self.record.feature_path, "diff", "--cached", "--name-only", "-z")
        if any(
            p and not (p == relative or p.startswith(relative + "/")) for p in staged.split("\0")
        ):
            raise GitError("Unrelated staged changes block plan commit")
        git(self.record.feature_path, "add", "--", relative)
        if git(self.record.feature_path, "diff", "--cached", "--name-only").strip():
            git(self.record.feature_path, "commit", "-m", message)
        return head(self.record.feature_path)

    def import_dependencies(self, worker: WorkerWorkspace, dependency_shas: list[str]) -> str:
        self._validate_worker(worker)
        if git(worker.path, "status", "--porcelain").strip():
            raise GitError("Cannot import dependencies into dirty worker worktree")
        for source in dependency_shas:
            source = self._sha(source)
            before = head(worker.path)
            completed = subprocess.run(
                ["git", "-C", str(worker.path), "merge", "--no-edit", source],
                check=False,
                capture_output=True,
            )
            if completed.returncode:
                paths = git(worker.path, "diff", "--name-only", "--diff-filter=U", "-z").split("\0")
                if not any(paths):
                    raise GitError(completed.stderr.decode(errors="replace"))
                raise GitConflict(
                    ConflictRecord(
                        str(worker.path), "merge", before, [source], [p for p in paths if p]
                    )
                )
        worker.baseline_sha = head(worker.path)
        if self.record:
            self.record.workers[worker.id] = worker
            self._save()
        return worker.baseline_sha

    def build_candidate(
        self, worker_shas: Mapping[str, str] | list[str], base_sha: str, cycle: int = 1
    ) -> Candidate:
        if not self.record:
            raise GitError("Prepare first")
        self._validate_record(self.record)
        keyed_sources = (
            [(str(key), worker_shas[key]) for key in sorted(worker_shas)]
            if isinstance(worker_shas, Mapping)
            else [(f"{index:08d}", sha) for index, sha in enumerate(worker_shas)]
        )
        # Preserve dependency ancestry even when worker IDs sort in the opposite
        # order. Independent commits use worker ID (or input position) as a stable
        # tie-breaker.
        normalized = [(key, self._sha(sha)) for key, sha in keyed_sources]
        unique = {}
        for key, sha in normalized:
            unique.setdefault(sha, key)
        nodes = [(key, sha) for sha, key in unique.items()]
        edges = {sha: set() for _, sha in nodes}
        indegree = dict.fromkeys(edges, 0)
        for _, earlier in nodes:
            for _, later in nodes:
                if earlier == later:
                    continue
                relation = subprocess.run(
                    ["git", "-C", str(self.repo), "merge-base", "--is-ancestor", earlier, later],
                    check=False,
                    capture_output=True,
                )
                if relation.returncode == 0:
                    edges[earlier].add(later)
                    indegree[later] += 1
                elif relation.returncode != 1:
                    raise GitError(relation.stderr.decode(errors="replace").strip())
        labels = {sha: key for key, sha in nodes}
        ready = sorted((sha for sha, degree in indegree.items() if degree == 0), key=lambda s: (labels[s], s))
        sources = []
        while ready:
            source = ready.pop(0)
            sources.append(source)
            for later in sorted(edges[source], key=lambda s: (labels[s], s)):
                indegree[later] -= 1
                if indegree[later] == 0:
                    ready.append(later)
                    ready.sort(key=lambda s: (labels[s], s))
        if len(sources) != len(nodes):
            raise GitError("Could not determine a deterministic source commit order")
        name = f"integration-{cycle}"
        branch = (
            f"orch/{self.record.feature_branch.removeprefix('feature/')}/{self.run_dir.name}/{name}"
        )
        path = self._owned_worktree(name, branch, self._sha(base_sha))
        journal_path = self.run_dir / f"{name}.json"
        progress = (
            json.loads(journal_path.read_text())
            if journal_path.exists()
            else {"base": self._sha(base_sha), "sources": sources, "merged": [], "head": head(path)}
        )
        if progress["sources"] != sources or progress["base"] != self._sha(base_sha):
            raise GitError("Candidate inputs changed; use a new cycle")
        if git(path, "status", "--porcelain").strip():
            raise GitError("Candidate has unfinished changes; resolver/recovery is required")
        current = head(path)
        if current != progress["head"]:
            # Reconcile only a completed, journaled merge whose result was not checkpointed.
            parents = git(path, "rev-list", "--parents", "-n", "1", current).split()[1:]
            pending = [sha for sha in sources if sha not in progress["merged"]]
            completed = [
                sha
                for sha in pending
                if current == sha
                or (len(parents) == 2 and parents[0] == progress["head"] and parents[1] == sha)
            ]
            if not completed:
                raise GitError("Candidate HEAD changed outside recorded integration operations")
            progress["merged"].extend(completed)
            progress["head"] = current
            atomic_write(journal_path, json.dumps(progress, indent=2))
        for source in sources:
            if source in progress["merged"]:
                continue
            before = head(path)
            atomic_write(journal_path, json.dumps(progress, indent=2))
            result = subprocess.run(
                ["git", "-C", str(path), "merge", "--no-edit", source],
                check=False,
                capture_output=True,
            )
            if result.returncode:
                paths = [
                    p
                    for p in git(path, "diff", "--name-only", "--diff-filter=U", "-z").split("\0")
                    if p
                ]
                if not paths:
                    raise GitError(result.stderr.decode(errors="replace"))
                conflict = ConflictRecord(str(path), "merge", before, [source], paths)
                atomic_write(self.run_dir / "conflict.json", json.dumps(asdict(conflict), indent=2))
                raise GitConflict(conflict)
            progress["merged"].append(source)
            progress["head"] = head(path)
            atomic_write(journal_path, json.dumps(progress, indent=2))
        return Candidate(head(path), path, sources, branch)

    def accept_resolution(
        self,
        conflict: ConflictRecord,
        covered_source_shas: list[str],
        allowed_paths: list[str] | None = None,
    ) -> str:
        """Record reviewed resolver coverage without requiring cherry-pick ancestry.

        The caller validates the resolver handover and its explicit source coverage first.
        Code and docs review still gate the resulting candidate's eventual promotion.
        """
        path = Path(conflict.path).resolve()
        resources = self.owned["resources"]
        names = [name for name, entry in resources.items() if Path(entry["path"]).resolve() == path]
        if len(names) != 1:
            raise GitError("Conflict worktree is not owned by this run")
        name = names[0]
        entry = resources[name]
        if self._registered_worktrees().get(path) != f"refs/heads/{entry['branch']}":
            raise GitError("Conflict resolver changed the target branch")
        coverage = [self._sha(sha) for sha in covered_source_shas]
        expected = [self._sha(sha) for sha in conflict.source_commits]
        if set(coverage) != set(expected):
            raise GitError("Resolver must explicitly cover every conflicting source commit")
        if git(path, "status", "--porcelain").strip():
            raise GitError("Resolver left uncommitted or unmerged changes")
        if git(path, "diff", "--name-only", "--diff-filter=U").strip():
            raise GitError("Resolver left unresolved entries")
        for operation in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
            metadata = Path(git(path, "rev-parse", "--git-path", operation).strip())
            if not metadata.is_absolute():
                metadata = path / metadata
            if metadata.exists():
                raise GitError("Resolver left an unfinished git operation")
        target = self._sha(conflict.target_head)
        resolved = head(path)
        ancestor = subprocess.run(
            ["git", "-C", str(path), "merge-base", "--is-ancestor", target, resolved],
            check=False,
            capture_output=True,
        )
        if ancestor.returncode:
            raise GitError("Resolver rewrote the pre-conflict target history")
        if resolved == target:
            raise GitError("Resolver supplied no resolution commit")
        # The resolver may edit source-touched files, not arbitrary surrounding code.
        expected_paths = set(conflict.paths)
        for source in expected:
            base = git(path, "merge-base", target, source).strip()
            expected_paths.update(
                p
                for p in git(
                    path, "diff", "--name-only", "--no-renames", "-z", base, source, "--"
                ).split("\0")
                if p
            )
        changed = changed_paths(path, target)
        for relative in changed:
            safe_relative(relative)
            within(path, relative)
            if (
                relative.casefold() == ".plans"
                or relative.casefold().startswith(".plans/")
                or relative not in expected_paths
            ):
                raise GitError(f"Resolver edited an unrelated or protected path: {relative}")
            if allowed_paths is not None and not any(matches(relative, p) for p in allowed_paths):
                raise GitError(f"Resolver edited outside approved scopes: {relative}")
        evidence = {
            "worktree": str(path),
            "target_head": target,
            "result_head": resolved,
            "covered_sources": coverage,
            "changed_paths": changed,
        }
        evidence_path = self.run_dir / f"{name}-resolutions.json"
        history = json.loads(evidence_path.read_text()) if evidence_path.exists() else []
        if not any(e["result_head"] == resolved for e in history):
            history.append(evidence)
            atomic_write(evidence_path, json.dumps(history, indent=2))
        if name.startswith("integration-"):
            journal_path = self.run_dir / f"{name}.json"
            if not journal_path.exists():
                raise GitError("Missing candidate progress journal")
            progress = json.loads(journal_path.read_text())
            if not set(coverage) <= set(progress["sources"]):
                raise GitError("Resolved source is outside candidate inputs")
            progress["merged"] = list(dict.fromkeys([*progress["merged"], *coverage]))
            progress["head"] = resolved
            atomic_write(journal_path, json.dumps(progress, indent=2))
        elif name.startswith("worker-") and self.record:
            worker_id = name.removeprefix("worker-")
            self.record.workers[worker_id].baseline_sha = resolved
            self._save()
        else:
            raise GitError("Conflict resolution is restricted to worker/integration worktrees")
        return resolved

    def promote_candidate(self, candidate: Candidate, expected_feature_head: str) -> str:
        if not self.record:
            raise GitError("Prepare first")
        self._validate_record(self.record)
        name = candidate.path.name
        entry = self.owned["resources"].get(name)
        if (
            not entry
            or entry["branch"] != candidate.branch
            or Path(entry["path"]).resolve() != candidate.path.resolve()
        ):
            raise GitError("Candidate is not owned by this run")
        if (
            head(candidate.path) != candidate.sha
            or git(candidate.path, "status", "--porcelain").strip()
        ):
            raise GitError("Reviewed candidate changed")
        feature = self.record.feature_path
        feature_head = head(feature)
        if git(feature, "status", "--porcelain").strip():
            raise GitError("Feature checkout must be clean before promotion")
        if feature_head == candidate.sha:
            # The fast-forward succeeded but the caller may have stopped before
            # persisting its stage transition. The exact reviewed candidate at the
            # feature tip is a safe, idempotent completion.
            return candidate.sha
        if feature_head != expected_feature_head:
            raise GitError("Feature HEAD changed since candidate review")
        journal_path = self.run_dir / f"{name}.json"
        if not journal_path.exists():
            raise GitError("Candidate has no recorded source coverage")
        progress = json.loads(journal_path.read_text())
        if (
            progress.get("head") != candidate.sha
            or progress.get("sources") != candidate.source_shas
            or not set(progress.get("sources", [])) <= set(progress.get("merged", []))
        ):
            raise GitError("Candidate source coverage is incomplete or changed")
        # Git's ff-only refuses integration if the candidate was built from a stale base.
        git(feature, "merge", "--ff-only", candidate.sha)
        return head(feature)

    def clean(
        self, ownership_record: WorkspaceRecord | None = None, *, active: bool = False
    ) -> None:
        if active:
            raise GitError("Cannot clean an active run")
        if ownership_record:
            self._validate_record(ownership_record)
        resources = self.owned["resources"]
        registered = self._registered_worktrees()
        # Validate all before removing any; dirty or externally changed resources stay intact.
        for entry in resources.values():
            path = Path(entry["path"]).resolve()
            if not path.is_relative_to(self.run_dir / "worktrees"):
                raise GitError("Cleanup path escaped owned directory")
            if path in registered:
                if registered[path] != f"refs/heads/{entry['branch']}":
                    raise GitError("Cleanup worktree branch changed")
                if git(path, "status", "--porcelain").strip():
                    raise GitError(f"Preserving unfinished/dirty worktree: {path}")
            elif path.exists():
                raise GitError("Unregistered cleanup path exists")
            if not entry["branch"].startswith(("feature/", "orch/")):
                raise GitError("Refusing protected branch cleanup")
            if entry["branch"].startswith("orch/") and git(
                self.repo, "show-ref", "--verify", f"refs/heads/{entry['branch']}", check=False
            ):
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.repo),
                        "merge-base",
                        "--is-ancestor",
                        entry["branch"],
                        self.record.feature_branch if self.record else "HEAD",
                    ],
                    check=False,
                )
                if result.returncode:
                    raise GitError(f"Preserving unmerged branch/worktree: {entry['branch']}")
        # Keep feature branches: they are the user's delivery output, not temporary resources.
        for name, entry in list(resources.items()):
            if name == "feature":
                continue
            path = Path(entry["path"]).resolve()
            if path in registered:
                git(self.repo, "worktree", "remove", str(path))
            if git(self.repo, "show-ref", "--verify", f"refs/heads/{entry['branch']}", check=False):
                # Only delete branches whose content is reachable from the feature branch.
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.repo),
                        "merge-base",
                        "--is-ancestor",
                        entry["branch"],
                        self.record.feature_branch if self.record else "HEAD",
                    ],
                    check=False,
                )
                if result.returncode:
                    raise GitError(f"Preserving unmerged branch: {entry['branch']}")
                git(self.repo, "branch", "-d", entry["branch"])
            del resources[name]
            self._save()
