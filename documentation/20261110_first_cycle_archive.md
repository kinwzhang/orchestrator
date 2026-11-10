# First-cycle archive and canonical project layout

Canonical project: /home/zwj808/PycharmProjects/orc2610_orchestrator, branch main.

Archive directory: /home/zwj808/PycharmProjects/orc2610_archives/20261110_first_cycle.

## Retained product files

Main contains the latest accepted code, tests, templates, packaging, requirement, essential product guides, current user manual, demo journey and enhancement requirements/backlog. Historical delivery folders are not merged into the product tree. Completed first-cycle .plans artifacts have been removed from the current tree after backup; they remain in Git history and the archives.

## Archive contents

- original_project_snapshot.tar.gz: original checkout files, including local modified/untracked material and historical analysis_and_reviews, before consolidation. Shared .git is excluded; Git history and index evidence are backed up separately.
- first_cycle_documents.tar.gz: documents-only archive of 2,351 files, including handovers, instructions, verdicts, planning records and historical guides.
- bootstrap_workspaces.tar.gz: complete bootstrap workspace snapshot, including worker/integration/documentation worktrees, handovers, reviewer records and untracked manuals.
- run_evidence.tar.gz: snapshots of predecessor and completed carry-forward run directories, including worktrees and evidence. Live platform-state copies are retained separately.
- repository_history.bundle: verified complete Git history/ref bundle captured before consolidation. Historical branches remain in the canonical repository as well.
- original_index.bin, original_index.patch, original_worktree.patch, original_status.txt, original_head.txt, original_branch.txt: original local staging/worktree context.
- worktree_inventory.json, branches.txt: original worktree/branch identities.
- Each tarball has a file/SHA-256 manifest. SHA256SUMS covers the archive package; README.md explains restoration and cleanup results.

## Inspect or restore

```bash
cd /home/zwj808/PycharmProjects/orc2610_archives/20261110_first_cycle
sha256sum -c SHA256SUMS
tar -tzf bootstrap_workspaces.tar.gz
```

Extract archives into a separate directory for inspection. Historical .git files contain old worktree paths; do not treat an extracted bootstrap tree as an already registered checkout. Use repository_history.bundle to restore history in an independent repository, or use retained branch refs to create a new worktree through Git. Do not overwrite current main, run state or global config to restore an old snapshot.

The two platform-state run directories remain in place for read-only status/history. A separately installed orch executable or already-open terminal may still refer to an old bootstrap path; start a new terminal in the canonical project and use uv run orch.
