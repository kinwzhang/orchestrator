# AGENTS.md

Guidance for every agent working in this repository. The coordinator's current instruction
package and approved feature plan override this file when they conflict. Keep
this file current as the code changes. First-cycle historical plans and handovers
are archived; see documentation/20261110_first_cycle_archive.md.

## What this repository is

This repository builds `orch`, a deterministic, Markdown-driven local delivery
orchestrator described by `requirement.md`. The first-cycle approved plan is
preserved in the archive documented in the archive index. Agents decide what to build
and whether it is good; the orchestrator decides when, in what order, and what
happens next. Every deliverable is a Markdown file. Internal JSON state is never an
agent input.

- Python >= 3.14, managed with `uv`; flat package under `src/orch/`.
- Markdown in, Markdown out: contracts are YAML front matter plus free-form body.
- Two supported platforms: native Fedora (bash) and native Windows/PowerShell 7.
  Never claim cross-platform verification from Linux-only evidence.

## Commands

- Refresh dependencies: `uv sync`
- Full test suite: `uv run pytest -q`
- Focused tests: `uv run pytest -q tests/contracts`
- Lint: `uv run ruff check .`
- CLI while developing: `uv run orch <command>`; validate a contract with
  `uv run orch validate <feature|manifest|discussion|handover|verdict> <file>`
- Tests use stub agents only. Never issue paid or live agent calls from tests.

Run both the tests and the lint check before declaring work finished, and record the
actual commands and results in your handover.

## Layout and ownership

Ownership lists in the current approved delivery manifest are exact. The table
below is the module map, not permission to revive retired first-cycle assignments.
Work only within your assigned paths; preserve uncommitted work you did not create. Route cross-module findings to the owning worker; shared fixtures and
dependency changes go through W01. Do not edit the plan, worker instructions, or
reviewer instructions.

| Area | Paths | Owner |
|---|---|---|
| Contracts, config, durable state, packaging, shared fixtures, this file | `src/orch/{__init__,contracts,config,store}.py`, `src/orch/templates/**`, `tests/contracts/**`, `tests/conftest.py`, `pyproject.toml`, `uv.lock`, `.gitignore` | W01 |
| Git worktrees, scope checks, candidate integration, conflicts, cleanup | `src/orch/git_ops.py`, `tests/git/**` | W02 |
| Agent adapters, process ownership, platform helpers | `src/orch/{processes,agents,platform_support}.py`, `tests/agents/**` | W03 |
| Workflow reducer, CLI, watch dashboard, reports, end-to-end tests | `src/orch/{__main__,workflow,cli,watch,reporting}.py`, `tests/{workflow,e2e,fixtures}/**` | W04 |
| Requirements, approved plan, delivery instructions | `requirement.md`, `analysis_and_reviews/**` | Coordinator only |

Module map: `contracts.py` (schemas, front matter, approval hashing, path rules),
`config.py` (TOML layers and precedence), `store.py` (atomic state, event journal,
Markdown evidence), `git_ops.py` (owned git resources), `processes.py`/`agents.py`
(subprocess and CLI adapters), `workflow.py` (stages, routing, budgets),
`cli.py`/`watch.py`/`reporting.py` (operator surface).

## Contracts and validation

- Front matter is validated strictly: unknown or duplicate keys, unsafe YAML tags,
  invalid enums, unsafe or traversing paths, and inconsistent verdicts fail with an
  error naming the file. `read_markdown` also enforces required body sections; a
  handover must contain an `## Interfaces and contracts` section.
- Approval records content hashes of every canonical plan input plus normalized
  effective settings and checks (`approval_hashes`, `detect_approval_changes` in
  `contracts.py`). Editing any approved input invalidates approval at the next
  stage boundary and requires `orch approve` again. Generated reports and approval
  metadata are excluded from the hashes.
- Configuration precedence is global TOML, then repository `.orch.toml`, then
  feature front matter. Relative configured paths resolve against the containing
  file, never the caller's working directory. Replacement always inherits the
  final worker model after all overrides.
- Domain errors carry one of six stable categories (`validation`,
  `approval_required`, `infrastructure`, `check`, `conflict`, `interrupted`);
  classify them with `error_category`. Adapters never choose lifecycle phases.
- Templates in `src/orch/templates/` validate against the schemas; placeholders
  are replaced before use.

## Durable state and physical evidence

- `StateStore` performs revision-checked atomic saves and journals events with
  stable IDs before side effects and again for results. Operation intents are
  merged with their completion so interrupted work can be reconciled.
- Every run continuously maintains `checklist.md`, `activity_log.md` and
  `issues.md` in its run directory. After a crash, regenerate Markdown from the
  state and journal instead of hand-editing; never duplicate event IDs.
- Do not hand-edit `state.json` or the journal to steer a run; use CLI commands
  and approved Markdown inputs.

## Safety rules

- Modify `main` only under explicit operator instruction. Never force-push or
  delete branches, worktrees, or files without ownership and operator authority. No automatic push, deployment, or external
  messaging. Cleanup is explicit and must refuse to discard unfinished or dirty
  work.
- Keep secrets out of logs, state, handovers, and reports; redact configured
  environment values before persistence.
- Preserve partial and uncommitted work across interruption; retain unfinished
  worktrees and Markdown evidence.

## Working rules for agents

- Start from files and finish with your named Markdown output; correctness never
  depends on chat history. Read dependency handovers before implementing.
- Do not commit, merge, change branches, delete worktrees, or launch additional
  agents unless your assignment explicitly says so.
- Issue budgets: each assignment gets up to three repair attempts and stops after
  two unsuccessful ones; each assignment gets three review rounds; an issue gets
  at most one reassignment; derived issues live at depth 0-2 and observing depth 3
  escalates the root immediately; a malformed verdict gets one repair attempt.
  Charge a failed repair once per actual worker repair outcome, never once per
  review invocation: approval-only revalidation of unchanged work must not
  consume a repair or replacement allowance. Preserve all historical counters.
- Handovers use front matter (`agent`, `round`, `status`, `files_changed`,
  `docs_changed`, `checks_run` with name/outcome/evidence) and body sections:
  Work completed; Decisions; Interfaces and contracts; Documentation relevance;
  Open issues; Verification; Continuation instructions. Report `partial` or
  `blocked` honestly; a blocked or partial handover never counts as completed
  work. Keep handover prose code-free: the documentation reviewer reads no code.
- New project documentation uses the existing convention: `YYYYMMDD` date prefix,
  lowercase words separated by underscores.

## Where things live

- Plan artifacts: `.plans/<slug>/` inside the target project's feature checkout
  (read-only for workers).
- Run data: platform state directory (`platformdirs`), outside every worktree;
  holds state, logs, handovers, verdicts and the Markdown evidence files.
- Worktrees: project changes only; never write notes or handovers into a worktree.
