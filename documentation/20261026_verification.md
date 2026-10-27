# Orchestrator verification record

## Scope and environment

This record distinguishes automated local evidence from platform acceptance.
The implementation session ran on Linux with Python 3.14 and `uv`; the
distribution was not independently confirmed as Fedora. No native Windows or
PowerShell 7 run and no live Codex/OpenCode model call was performed.

## Automated checks

| Command | Result | Evidence |
|---|---|---|
| `uv run pytest -q` | Pass | 121 tests passed in 5.19 seconds on the final run. |
| `uv run ruff check .` | Pass | Ruff reported no findings on the final run. |
| `uv run orch --help` | Pass | CLI parsed and displayed all registered lifecycle and validation commands. |

The deterministic tests use temporary repositories and stub agents. W04 coverage
includes complete delivery through candidate review and feature promotion,
approval invalidation after instruction or effective-model edits, the explicit
human resume gate, planner pause and abort, retained partial worker changes,
worker commit recovery after a simulated crash before checkpoint, detection of
post-CHECK worktree mutation, sibling cancellation with preserved leaf error
details from an exception group, issue lineage/budgets, and safe dashboard
rendering of untrusted output. These tests do not evaluate LLM judgment.

## Platform gates still required

- Run `uv run pytest -q` and `uv run ruff check .` natively on Fedora and record
  OS, Python, Git and CLI versions.
- Run the same suite natively on Windows with PowerShell 7. Exercise command
  shims, paths with spaces/Unicode, file replacement and locks, process-tree
  cancellation, terminal fallback and cleanup before claiming Windows support.
- Perform separately authorized real-agent smoke tests for interactive Codex
  planning and OpenCode worker/reviewer calls. No credentials or model calls
  were used for this verification record.
- The watch dashboard is a read-only view; native agent-session attachment is
  not enabled. Documentation-only reviewer inputs and prompts are curated, but
  are not an OS-level filesystem sandbox.
- Crash recovery can restore only persisted state, written discussion decisions
  and output bytes. Data that an agent had not written before interruption is
  not recoverable.
