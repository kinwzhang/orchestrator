# Git workspaces and recovery

The orchestrator creates a dedicated `feature/<slug>` branch and canonical checkout before plan approval. It never switches the user's current checkout. Worker branches and integration candidates use the `orch/<slug>/<run-id>/...` namespace. An existing branch or path without a run ownership record is never adopted.

## Persistent locations

`GitWorkspace(repo, run_dir)` stores ownership and workspace recovery metadata in `git_ownership.json` and `workspace.json` in the external run directory. Worktrees are under `run_dir/worktrees/`. Their code changes, staged files and untracked files remain on disk on interruption. The feature's canonical plan directory is `<feature-checkout>/.plans/<slug>/`. Agents receive the canonical directory explicitly; worker copies are not their instruction source.

Ownership intent is atomically persisted before git creates resources. The recorded start commit remains the feature and worker baseline if a crash occurs after worktree creation but before the workspace record is saved, even if the configured base branch advances before recovery. On a restart the manager reconciles a matching branch/worktree with the intent. A changed branch, unexpected path, missing registered worktree, or mismatched repository raises an error rather than guessing. Recovery never performs a reset, stash, clean, force checkout, or force push.

## Scope and checkpoints

`inspect_changes` includes tracked changes against a worker baseline, staged/unstaged changes, untracked nonignored files, deletions, binary changes, and both endpoints of a rename. Scopes use repository-relative POSIX patterns. The check rejects plan-folder changes, symlink escapes, case-colliding paths, and paths outside a supplied allowlist. A fingerprint captures changed content so changes to an already checked path cannot pass through an old scope-check result.

`commit_worker` requires a change set produced with the worker's declared allowed paths, then rechecks both scope and content fingerprint immediately before staging. It stages only those validated paths and creates an orchestrator commit. An unexpected worker HEAD is an error: workers should not manage git history themselves. The workflow persists a commit intent before the git side effect and reconciles a crash-after-commit only when the new commit has the expected baseline parent and matches the validated scope and fingerprint. After reviewing a committed delivery, the workflow advances the baseline explicitly before the next repair. The manager does not infer that a worker-created commit is approved.

Dependency branches are merged into a clean dependent worktree before its assignment starts. The resulting HEAD becomes its baseline. Its imported dependency changes therefore do not count as its own scope changes. Dependency updates require stopping the dependent and reconciling its existing changes before importing again; dirty work is preserved.

## Integration and conflict resolution

The manager builds a separately owned integration branch/worktree, combining worker commits in dependency ancestry order, with stable worker-ID order for independent commits. Merging source commits retains ancestry, preventing duplicate dependency changes. Each integration cycle has a persistent input/progress journal; a completed merge whose progress update was interrupted is reconciled from its exact parent/source ancestry on retry. Promotion requires the journal to record the exact candidate head and complete source coverage. A changed input set needs a new candidate cycle.

A conflict raises `GitConflict`, whose record identifies the affected worktree, in-progress operation, prior HEAD, source commits, and unmerged paths. The workflow launches the separate conflict-resolution agent from this record and Markdown handovers. The manager does not call agents or decide how source conflicts should be resolved. The resolver can abort the pending merge and cherry-pick the supplied changes where appropriate; its resolution must be checked and reviewed as part of the final candidate. Uncertainty escalates to the user. After the workflow validates the handover, `accept_resolution` requires explicit coverage of the conflicting sources and checks ownership, clean committed state, target-history retention and allowed changed paths. It records cherry-picked source coverage in the candidate journal so replay does not apply those sources again. For dependency conflicts it updates the retained worker baseline.

`promote_candidate` verifies candidate ownership, unchanged reviewed HEAD, complete journaled source coverage, a clean candidate, the expected feature HEAD, and a clean feature checkout. It then performs a fast-forward-only merge into the feature branch. If a crash occurs after that fast-forward but before the workflow records completion, retry succeeds only when the exact candidate is already the clean feature tip. A feature checkout switched to another branch is rejected. No method merges into main. Source reviews and acceptance decisions are the workflow's responsibility; callers must not promote before both reviews pass for the candidate.

Plan-only commits stage paths only beneath the canonical `.plans/` folder. Unrelated staged changes prevent them from accidentally entering a metadata commit.

## Cleanup

Cleanup is an explicit operation, forbidden for an active run. It validates all recorded paths and branch identities before removing worktrees. Dirty or unmerged worker/candidate branches block cleanup, preserving their worktrees and implementation. Clean temporary worktrees and branches may be removed only if reachable from the delivered feature branch. The feature branch and canonical feature checkout are retained as the user's output. User-created branches, worktrees, and the original checkout are untouched.

## Verification and limits

Run `uv run pytest -q tests/git` for temporary-repository tests of recovery, scope, dependency import, conflicts, promotion, and cleanup. Tests include directories containing spaces and native git operations without shell syntax. Windows-specific file locking, long-path behavior, and symlink privileges still require native Windows verification. These checks are safety checks for cooperative local agents, not protection against an adversary modifying ownership state or arbitrary user files.
