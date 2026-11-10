# Local orch user manual

The original project folder is the canonical main checkout after first-cycle consolidation. Use it for the current implementation; old bootstrap paths are historical.

## Run from the canonical checkout

```bash
cd /home/zwj808/PycharmProjects/orc2610_orchestrator
uv sync --group dev
uv run orch --help
```

Plain orch may refer to a separate installed version. uv run selects this checkout. Target projects can live elsewhere.

## Create, plan and approve

Use a new slug for genuinely new work and provide the target, requirement and existing base branch explicitly. Example command shape:

```bash
uv run orch new my-feature --target /path/to/project --requirement /path/to/requirement.md --base main
uv run orch plan my-feature
uv run orch approve my-feature
```

Discuss decisions and inspect the generated plan before approval. Canonical plan files are in the owned target feature checkout. Run evidence is outside source worktrees under the platform state directory. On this Fedora setup it is /home/zwj808/.local/state/orch.

## Execute and observe

```bash
uv run orch --data-dir /home/zwj808/.local/state/orch next my-feature
uv run orch --data-dir /home/zwj808/.local/state/orch status my-feature
uv run orch --data-dir /home/zwj808/.local/state/orch watch my-feature
```

In step mode, next advances one stage and can wait for agents. Use status/watch from another terminal. Keep only one writer command active per run. Checks and independent reviews precede the human-test gate. A completed imported historical issue must not be redispatched as a fresh repair.

## Change model preferences

```bash
uv run orch models show
uv run orch models set code_reviewer --model opencode-go/muse-spark-1.3-contributor --reasoning medium --scope global
```

Global settings live at ~/.config/orch/config.toml. Precedence is global, repository .orch.toml, then feature settings. Current worker preference is DeepSeek V4.1 Flash; replacement inherits the worker model with its configured reasoning override. The active agent keeps its invocation settings. A later boundary detects approval changes; approve again when required. Direct approve while RUNNING can be refused until the boundary records invalidation or the run is in an allowed approval phase.

## Human acceptance

At AWAITING_TEST, test the delivered feature worktree. For a CLI, exercise the commands and check output. For a web app, start the local application and exercise its UI against acceptance scenarios. Then, if satisfied:

```bash
uv run orch --data-dir /home/zwj808/.local/state/orch done my-feature
```

Done records acceptance. Main merges, publication, deployment and cleanup are separate actions. For changes after acceptance, inspect orch reopen --help and supply the required notes; do not rewrite historical evidence.

## First-cycle reference

model-preferences-cli-carry-1-f984dda7 is DONE. The predecessor model-preferences-cli-5ec7f5ee remains preserved in its stopped historical state. Do not resume it casually or reset its counters. Both runs remain in platform state storage, with independent backup archives.

Current runbook details remain in the linked lifecycle operations and model-preferences guides. Historical worker handovers, reviewer packets, deprecated manuals and bootstrap source snapshots are in the first-cycle archives. No external run data is deleted by project-folder consolidation.
