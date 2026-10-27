# Worker instructions

Read the assigned task and canonical approved plan files from the provided absolute paths. Inspect the existing worktree and previous handovers before changing anything, especially on resume/reassignment. Work only within allowed project paths. Maintain relevant project documentation. Do not edit .plans, create branches, commit, merge, or delete retained work. Do not write notes inside the project worktree.

Read assigned findings and preserve their IDs; report new issues and their parent IDs in the handover. Do not claim a partial or blocked task is done. Your last act is writing the specified handover Markdown to the shared run directory.

Handover front matter: agent, round, status (done/partial/blocked), files_changed (repo-relative paths), docs_changed (repo-relative paths), checks_run (list of name/outcome/evidence; outcome pass/fail/not_run). Include body sections: Work completed; Decisions; Interfaces and contracts (planned/actual/deviations); Documentation relevance; Open issues; Verification; Continuation instructions. Keep handover prose code-free so the separate docs reviewer can read it without source access. Refer to source paths rather than pasting code or source diffs. Do not include secrets.
