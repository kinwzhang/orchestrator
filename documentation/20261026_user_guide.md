# Orchestrator user guide

`orch` runs a Markdown-defined delivery workflow against a local Git repository.
The planner, workers and reviewers make project decisions; `orch` controls the
stage order, checks, owned worktrees, approvals, retries and evidence. No agent
receives internal JSON state.

## Install and configure

Use Python 3.14 or newer and `uv` on Fedora or native Windows with PowerShell 7.
From this repository, install the locked environment and run the CLI with
`uv sync` and `uv run orch --help`. For a packaged local installation, build or
install the project with `uv tool install .`; the `orch` entry point is exposed
by the project metadata. Install and authenticate Codex and OpenCode separately.
The selected CLI versions and role models must support the configured roles;
`orch` probes CLI version/help capabilities but does not test provider billing
or make a model call during capability probing.

Configure the global `config.toml` using the platform configuration directory,
or add repository settings to `.orch.toml`. Set explicit planner, worker,
code-reviewer, docs-reviewer and conflict-resolver models. Replacement inherits
the final effective worker model. Model IDs in examples elsewhere are
placeholders. Never put credentials in these files. See
`20261026_contracts_and_configuration.md` for precedence, settings, approval
hashes and redaction guidance.

## Create and plan a feature

From the target repository, create a feature and its dedicated feature checkout:

```text
orch new <slug>
orch new <slug> --target <repository-path> --requirement <markdown-path> --base <branch>
```

`orch new` defaults the target to the current directory, requirement to
`requirement.md`, and base branch to `main`. It prints the editable
`.plans/<slug>/feature.md` path in the owned feature checkout. The plan directory
contains the requirement snapshot, discussion, plan, manifest, reviewer
instructions and worker instructions. Edit the feature settings and supply
valid, complete plan artifacts before approval.

Start or continue the interactive Codex planning discussion with:

```text
orch plan <slug>
```

The files are authoritative across sessions. Codex should keep
`discussion.md` current as decisions are made. An interrupted discussion can
resume from those saved files, but details not yet written to disk cannot be
recovered. Once the discussion and required artifacts validate, inspect them
and explicitly approve:

```text
orch approve <slug>
```

Approval pins the canonical Markdown inputs and effective execution settings.
Any changed approved input, including a model or check configuration, requires
another approval before work continues.

## Run, inspect and steer

```text
orch run <slug>
orch next <slug>
orch status [<slug>]
orch watch [<slug>] [--once]
```

`run` proceeds to the next human gate unless the feature selects `step` mode.
In `step` mode it performs one stage. `next` performs one correct next action
and never skips approval, escalation or final testing. `status` reports the
phase, stage, workers and blockers. `watch` is a read-only terminal dashboard;
`--once` prints a single snapshot. Logs and the dashboard are best-effort views,
not control inputs.

The run directory is outside project worktrees. It contains machine state,
sanitized logs and the continuously refreshed `checklist.md`,
`activity_log.md` and `issues.md`. Plans live in the target feature checkout;
workers change only their assigned scopes in retained worktrees. Dependencies
run topologically, and integration checks and both reviewers gate promotion of
one candidate SHA to the feature branch. Main is not a promotion target.

Pause, resume or abort an active or planning session with:

```text
orch pause <slug>
orch resume <slug>
orch abort <slug>
```

Pause and abort requests are persisted for the active process to consume.
Interactive planner termination is process-tree scoped and leaves written
discussion content in place. Resume continues from persisted stages and
retained worktrees; abort retains evidence and does not resume automatically.
Use `status` and `watch` to confirm the resulting state.

After successful code/docs review and promotion, test the feature branch and
close the human gate with:

```text
orch done <slug>
orch reopen <slug> <notes-file>
```

`reopen` snapshots the notes into the plan and starts another bounded cycle.
`orch clean <slug>` is explicit and available only for DONE or ABORTED features;
it refuses dirty or unfinished owned work. Never remove branches or worktrees
manually while a run is unfinished.

Validate contract Markdown without starting agents:

```text
orch validate feature <file.md>
orch validate manifest <file.md>
orch validate discussion <file.md>
orch validate handover <file.md>
orch validate verdict <file.md>
```

## Safety and platform boundaries

The orchestrator does not push, deploy, send arbitrary external messages, or
merge into `main`. Scope checking and agent permissions are safety controls, not
an operating-system security sandbox. Secret redaction covers configured values
and recognizable token patterns, not every possible secret; never provide
credential files as inputs. Preserve the run directory and worktrees together
when backing up or restoring an unfinished run.

Linux test results do not establish native Fedora terminal behavior or native
Windows process, shim, filesystem, PowerShell and locking behavior. Native
Windows acceptance requires running the documented verification suite on
Windows with PowerShell 7. Agent-native session attachment is not enabled; use
the read-only watch dashboard and sanitized logs.

## Related documentation

- `20261026_contracts_and_configuration.md` — schemas, settings and durable state
- `20261026_git_and_recovery.md` — worktrees, integration and recovery
- `20261026_agents_and_platforms.md` — adapters, processes and platform helpers
- `20261026_verification.md` — commands, evidence and unverified platform gates
