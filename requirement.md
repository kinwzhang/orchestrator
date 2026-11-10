# Orchestrator: Requirements

## 0. How to work with this document (instructions to Codex)

You are the planner and builder of this project. Do not write code yet.

1. Read this document fully, then read the repo.
2. Discuss with me: list your questions, ambiguities, and risks. Challenge any requirement you think is wrong or over-engineered. Section 14 lists decisions I want your recommendation on.
3. Produce an implementation plan (modules, file layout, milestones, test strategy) as a markdown file and wait for my approval.
4. Only then build, milestone by milestone, with tests.
All of your deliverables in this project, and in the runs of the orchestrator you are building, are markdown files (see principle P1).

## 1. Purpose

Build a standalone **orchestrator**: a deterministic program that automates a multi-agent software delivery loop. It takes a feature requirement, has a planner agent produce instructions, runs worker agents in parallel git worktrees, has reviewer agents validate the code and the documentation, and loops until the reviewers approve or the loop escalates to me. I can watch the live status of all agents in a terminal.

Agents decide *what* to build and *whether it is good*. The orchestrator decides *when*, *in what order*, and *what happens next*. The orchestrator itself never judges code quality.

## 2. Design principles

- **P1. Markdown in, markdown out.** Every deliverable an agent produces (Codex's plan, instructions, manifest; workers' handovers; reviewers' verdicts) is a markdown file. Where the orchestrator must read a field, the file has a strictly validated YAML front matter block followed by free-form markdown body. The orchestrator acts on the front matter; humans and agents read the body.
- **P2. Agents act on files, never on chat.** Every agent starts from an instruction file produced by the previous stage and writes its result to a named output file. The orchestrator hands over file paths, not pasted content. No stage depends on conversational context carried from another stage.
- **P3. Handover documents.** Every transition between agents goes through a handover document (section 6). A fresh session, a replacement worker, or a reviewer must be able to continue from the handover alone.
- **P4. Documentation is part of the work.** Workers maintain the documentation relevant to their change as part of the task. A change is not done until its documentation is current.
- **P5. The orchestrator is a program, not an LLM.** It owns the state machine, git, processes, retries, escalation, and notifications. Its own run state may be machine formats (JSON); everything agents read or write is markdown.
- **P6. Observable.** I can see what every agent is doing at any time without interrupting the run.
## 3. Roles

| Role | Tool | Responsibility |
|---|---|---|
| Planner | Codex (headless, e.g. `codex exec`) | Understand the target repo, discuss and understand the requirement with me, write the plan, worker instructions, reviewer instructions, doc-reviewer instructions, and manifest, all as markdown |
| Workers | OpenCode (headless, e.g. `opencode run`, or its server/SDK) | Implement the assigned task inside their own worktree, maintain the relevant documentation, write a handover |
| Code reviewer | OpenCode (separate session) | Validate worker results against the requirement and acceptance checks, write a verdict |
| Doc reviewer | OpenCode (separate session, standalone agent) | Check that documentation is relevant, accurate, and complete relative to the changes, write a doc verdict |
| Orchestrator | This project (not an LLM) | State machine, git, process management, retries, escalation, live status, notifications |
| Me | | Give the requirement, approve the plan, test the final result, handle escalations |

Rationale: OpenCode gives more tokens, so it does the bulk of the work. Codex is used where its planning strength matters most.

## 4. Pipeline

```
REQUIREMENT -> PLAN -> (human approves plan) -> PREPARE -> WORK -> CHECK -> REVIEW (code + docs in parallel)
                                                             ^                         |
                                                             |   rework / reassign     |
                                                             +-------------------------+
Both reviews pass -> MERGE -> NOTIFY (human tests)
Round limit hit, or unrecoverable error -> ESCALATE (human)
```

### 4.1 PLAN
- Input: my requirement text plus the target repo.
- Codex reads the repo structure, discusses the requirement with me interactively, then writes the plan artifacts (section 5).
- I explicitly approve the plan before anything proceeds.
### 4.2 PREPARE
- Create the feature branch from the configured base branch.
- Create one git worktree per worker, on its own branch derived from the feature branch.
- Record base commit and paths in the run state.
### 4.3 WORK
- Launch all workers in parallel (configurable concurrency limit), each in its own worktree, each told to read its own instruction file (and, on rework, its previous handover and the review feedback addressed to it).
- Each worker implements its task, updates the relevant documentation within its allowed paths, and writes its handover document (section 6) as its last act.
- Capture per-worker logs, exit status, and duration. Support timeouts and clean termination.
### 4.4 CHECK (deterministic, no LLM)
- Per worktree, run the configured commands (test, lint, typecheck).
- Verify each worker's diff stays within its declared scope (allowed paths in the manifest). A scope violation counts as a failure.
- Verify the worker produced a valid handover document. A missing or malformed handover counts as a failure.
- Failures go straight back to the worker with the failure output, without consuming reviewer tokens.
### 4.5 REVIEW
- Once all workers pass CHECK, the code reviewer and the doc reviewer run (in parallel by default), each as its own session, each starting from its instruction file plus the worker handovers.
- The code reviewer validates against the requirement, plan, and acceptance checks. The doc reviewer validates documentation (section 7).
- Each writes a verdict file (section 8). The orchestrator acts only on the verdict front matter.
### 4.6 Rework loop
Per the verdicts, for each finding:
- Route back to the **same worker** with the findings, or
- **Reassign** to a different worker (fresh session, continuing from the handover documents) if the verdict says so, or if the same worker has failed the same finding twice (configurable).
- The worker produces a new handover. Then re-run CHECK and REVIEW. Repeat.
### 4.7 Termination
- **Approve:** both verdicts `pass`. Merge worker branches into the feature branch (never into main), write the final report, notify me.
- **Escalate:** max rounds reached (configurable, default 3), a worker hits max retries, repeated infrastructure failures, or a reviewer returns `escalate`. Write a report of the state of every worker, the open findings, and the history, then notify me.
## 4A. Feature lifecycle, gates, and control

The orchestrator is operated by commands and by editing files, never by editing its source code. I must be able to stop at any point, close the terminal or shut down the machine, and come back later to continue exactly where I left off.

### 4A.1 Feature as the unit of work

Each feature has a `feature.md` at `.plans/<slug>/feature.md`, edited by me. Front matter holds the settings: target project path, requirement file path, slug, base branch, and optional overrides (max rounds, models, run mode). The body is free notes. `orch new <slug>` creates it from a template. Nothing about a feature is hard-coded in the orchestrator.

The orchestrator keeps a per-feature state record (machine format, outside the agents' view) with the current phase:

```
DRAFT -> DISCUSSING -> PLAN_READY -> APPROVED -> RUNNING -> AWAITING_TEST -> DONE
                                                     |
                                                 ESCALATED   (waits for me)
```

`PAUSED` and `ABORTED` are possible from any active phase. Phase transitions are logged with timestamps.

### 4A.2 Human gates

The orchestrator stops and waits for me at these points and nowhere else (in `auto` mode):

1. **Plan approval.** When Codex finishes the plan documents the phase becomes `PLAN_READY`. Nothing runs until I run `orch approve <slug>`. I may edit any plan file by hand in between. The orchestrator records a hash of every plan file at approval; if any file changed afterward, it reports the change and asks for re-approval before continuing.
2. **Escalation.** On round limit, retry limit, or a reviewer's `escalate`, the phase becomes `ESCALATED` with a report. I decide: edit instructions and `orch resume`, or abort.
3. **Final test.** When both reviewers pass and branches are merged into the feature branch, the phase becomes `AWAITING_TEST` and I am notified. `orch done <slug>` closes the feature. `orch reopen <slug> <notes-file>` sends it back with a new requirement or notes file, which starts a new round of the loop (notes are handed to agents as a file, per P2).
### 4A.3 Run modes

- `step`: stop after every stage and wait for `orch next <slug>`. For building trust in the tool.
- `auto` (default): run to the next gate.
`orch next <slug>` always performs the correct next action for the feature's current phase, so I never have to remember where I was.

### 4A.4 Stopping and resuming the Codex discussion

The planning discussion is interactive and may span several sessions. Continuity must not depend on Codex's own chat history.

- Codex is instructed to maintain a living `.plans/<slug>/discussion.md` throughout the discussion: decisions made, open questions, assumptions, and its current understanding of the requirement and the repo. It updates this file as the discussion progresses, not only at the end.
- When I return (`orch plan <slug>` in `DISCUSSING`), a new Codex session starts from the requirement file, the repo, and `discussion.md`, then continues. If Codex supports resuming its previous session, the orchestrator may use that as a bonus, but correctness never depends on it.
- The phase moves to `PLAN_READY` only when Codex has written all plan documents and listed no open blocking questions in `discussion.md`, or when I explicitly declare the discussion finished.
### 4A.5 Stopping and resuming a run

- `Ctrl+C` or `orch pause <slug>` performs a graceful stop: terminate or let finish running agents (configurable), record state, and exit. A hard kill or crash must also be recoverable.
- `orch resume <slug>` skips every completed stage and completed worker. An interrupted worker restarts from its instruction file; the wrapper prompt tells it to inspect its worktree and any partial handover before acting, and continue rather than start over.
- Worktrees, branches, plan files, handovers, and logs persist between sessions. Closing the terminal or rebooting loses nothing.
- Multiple features may exist at the same time in different phases; only one is active in a run at a time (simultaneous runs remain out of scope).
### 4A.6 Steering without restarting

Because agents act only on files, I can steer by editing files between stages: change a worker's instruction file, edit `reviewer.md` or `doc-reviewer.md`, or drop a note file into the next round's inputs. The orchestrator picks the changes up at the next stage start and records in the run log that an instruction file changed. It does not require a restart.

### 4A.7 Safety limits

Per-run budgets (max rounds, max worker retries, per-agent timeout, optionally a max total runtime) apply in every mode so that an unattended run cannot loop indefinitely.

## 5. Plan artifacts (written by Codex, all markdown)

Stored in the target repo under `.plans/<slug>/` so they travel with the branch:

```
.plans/<slug>/
  feature.md            # my settings for this feature (section 4A.1)
  requirement.md        # the agreed requirement
  discussion.md         # living record of the planning discussion (section 4A.4)
  plan.md               # design summary
  manifest.md           # machine-readable contract: YAML front matter + notes
  reviewer.md           # code reviewer instructions
  doc-reviewer.md       # doc reviewer instructions
  workers/<id>.md       # one instruction file per worker
  report.md             # final report, written by the orchestrator at the end
```

Run data (handovers, verdicts, logs, state) is **not** stored here. See section 5A.

### manifest.md front matter (v1, to be refined with Codex)

```yaml
---
version: 1
slug: feature-slug
branch: feature/feature-slug
base_branch: main
workers:
  - id: w01
    title: short title
    instruction: workers/w01.md
    allowed_paths: ["src/module_a/**", "tests/module_a/**", "docs/module_a.md"]
    depends_on: []
reviewers:
  code: reviewer.md
  docs: doc-reviewer.md
checks:
  - {name: tests, cmd: "pytest -q"}
  - {name: lint, cmd: "ruff check ."}
limits:
  max_rounds: 3
  max_worker_retries: 2
  worker_timeout_min: 45
---
Free-form notes for humans below the front matter.
```

Rules for the plan:

- Workers are partitioned so their code scopes do not overlap.
- Every worker's `allowed_paths` includes the project documentation paths it is responsible for.
- No worker's `allowed_paths` may include the plan folder (`.plans/`). The scope check enforces this, so a worker cannot modify its own instructions.
- `depends_on` allows ordering where workers cannot run fully in parallel (see section 5A.4).
- **Cross-worker interfaces are defined up front.** `plan.md` must contain an "Interfaces" section specifying every contract one worker produces and another consumes (names, signatures, data shapes, file formats, ownership). Workers build to that contract rather than negotiating it live.
## 5A. Where files live

Three locations, with different rules.

### 5A.1 The plan folder: in the target project, canonical

- `.plans/<slug>/` lives in the **target project**, never in the orchestrator project. The orchestrator project contains only the tool (code, templates, docs for the tool itself).
- The canonical copy is the one in the **main feature-branch checkout** of the target project. The orchestrator gives every agent the **absolute path** of this canonical plan folder. Agents never read plan files from their own worktree copy, so edits I make between stages reach every agent.
- The plan folder is **read-only for workers** (excluded from their allowed paths, detected by the scope check).
- On first use, the orchestrator copies my requirement file (which may live anywhere) into `requirement.md`, so the plan folder holds a snapshot.
- **Committing the plan files** on the feature branch is configurable (`commit_plan: true | false`, default `true`), because an approved plan is part of the feature's history and lets me continue the discussion from my other machine. A second option (`strip_plan_on_merge`) controls whether `.plans/` is removed when the feature merges onward.
### 5A.2 The run directory: shared, outside every worktree

- Run data lives in a **run directory** that is not inside any worktree and not inside the orchestrator's source tree: a configurable data directory (default per platform, e.g. under the user's data or state directory on Fedora and Windows). Paths of the form `runs/<run-id>/...` in this document are relative to it.
- It holds: handover documents, verdicts, interface notes, per-agent logs, raw agent output, `state.json`, and the live status files.
- It is **shared**: the orchestrator passes its absolute path to every agent. Anything an agent writes here is visible to the reviewers, to other workers, and to replacement workers, and survives worktree cleanup.
- Handovers and verdicts are an audit trail but are noise in a repo. By default they stay in the run directory and the final `report.md` summarizes them. A config switch `commit_run_history: true` copies them into the plan folder at the end for a full history.
### 5A.3 Worktrees: private, hold project changes only

- Each worker's worktree and branch contain **only the project change**: code, tests, and the **project documentation** (README, docs/, docstrings, changelog) for that change. Project documentation is part of the deliverable, so it lives with the code it describes, inside the worker's allowed paths, and is committed on the worker's branch.
- Workers do **not** write handovers, status notes, or working notes into their worktree.
- Others see a worker's project documentation through the diff after CHECK, and through the worker's handover, which lists what was changed.
### 5A.4 Cross-worker visibility during a run

- **Handovers are readable by every agent in the run.** A worker may read other workers' handovers to learn what they have built or decided.
- **Interface notes:** a worker that creates or changes something others depend on records the actual contract in its handover (or in `interfaces/<worker-id>.md` in the run directory). Where this deviates from the planned Interfaces in `plan.md`, the worker flags the deviation and the orchestrator surfaces it in the next review.
- **Dependency ordering:** for workers with `depends_on`, the orchestrator runs the dependency first, then merges (or rebases) its branch into the dependent's worktree before starting the dependent, so the dependent sees the real code and documentation rather than only a description.
## 6. Handover documents

Every agent that finishes a unit of work writes a handover document to the shared run directory (section 5A.2), at `runs/<run-id>/round-<n>/handover/<agent-id>.md`, never into its worktree. The orchestrator validates the front matter; the body is for the next agent and for any other agent that needs it.

Worker handover, required content:

- Front matter: `agent`, `round`, `status` (`done | partial | blocked`), `files_changed`, `docs_changed`, `checks_run`.
- Body: what was done against the instruction; key decisions and why; what was deliberately not done; open issues and risks; how to verify; which documentation was updated or intentionally left unchanged, with reasons.
Rules:

- A handover must be sufficient for a fresh agent to take over the work without any other context.
- Handovers are readable by all agents in the run (section 5A.4). Each worker handover includes an "Interfaces and contracts" section for anything others depend on, stating the actual contract and any deviation from `plan.md`.
- Reviewers read the handovers first, then the diffs.
- On rework or reassignment, the receiving worker reads: its instruction file, the previous handover(s), and the review feedback addressed to it. Nothing else is passed.
- Reviewers' verdicts act as the handover back to workers (section 8).
## 7. Documentation requirements

- **Workers maintain documentation themselves.** Relevant project documentation (README, docs/, docstrings, changelog, config references, examples) is updated in the same change as the code, in the worker's worktree, within its allowed paths (section 5A.3). This is distinct from handovers and working notes, which go to the shared run directory.
- **Doc reviewer** is a standalone agent (separate session, separate instructions in `doc-reviewer.md`) so that documentation is judged independently of the code review. It checks:
  - documentation affected by the change was updated (nothing stale or contradicting the new behavior)
  - new behavior, configuration, commands, and interfaces are documented
  - what is written is accurate against the code (examples and commands plausibly work)
  - the documentation is relevant: no noise, no duplication, correct location
  - handover docs correctly report documentation changes
- Whether the doc review is a separate agent or an extra section of the code reviewer's job is configurable. The default is a separate agent. (Decision 4 in section 14.)
## 8. Verdict documents (written by reviewers, markdown)

`runs/<run-id>/round-<n>/verdict-code.md` and `verdict-docs.md`. Front matter is strictly validated:

```yaml
---
reviewer: code | docs
result: pass | rework | escalate
findings:
  - id: f1
    worker: w02
    severity: blocker | major | minor
    action: rework_same_worker | reassign | escalate
    summary: one line
---
## Summary
...
## Findings
Detail per finding, with file and line references and what to do.
## Manual test notes
What the human should try.
```

A malformed verdict triggers one retry of that reviewer with the validation error; a second failure escalates. Findings are written so that a worker can act on them from the file alone (P2, P3).

## 9. Live status and observability

- The orchestrator continuously writes machine-readable run status and per-agent logs to the run directory.
- **`orch watch [run-id]`** shows a live terminal dashboard: current stage and round, each agent (workers, reviewers) with state, elapsed time, last activity, a tail of its output, check results, and retry counts. It is read-only, can be started or stopped at any time, and can attach to a run already in progress.
- **Launching a viewer terminal:** `orch run` can open the watch dashboard in a new terminal window or split pane automatically (Windows Terminal with PowerShell 7 on Windows; on Fedora, tmux or the default terminal emulator), configurable, and must degrade gracefully to printing the command to run manually if that is not possible.
- **Optional per-agent live view:** a mode that opens one pane per agent showing its live stream, if the platform allows it (tmux on Fedora, Windows Terminal split panes via `wt` on Windows). If OpenCode offers a way to attach to a running session interactively, support attaching to a specific agent from the dashboard. Research feasibility and report back.
- Logs are plain text files, so `tail -f` or any editor works as a fallback.
## 10. State and resumability

- All run state lives on disk (`runs/<run-id>/state.json`, per-round directories, logs, handovers, verdicts). Nothing important lives only in memory.
- If the orchestrator crashes or I stop it, `resume` continues from the last completed stage without redoing finished work.
- State transitions are logged with timestamps.
## 11. Agent invocation layer

- Isolate agent CLI calls behind a small adapter interface (`run(instruction_file, output_file, cwd, model, timeout) -> result`), with one adapter for Codex and one for OpenCode, so CLI changes or a different agent do not ripple through the codebase.
- The orchestrator builds the prompt for each agent as a short wrapper: read this instruction file, follow it, write your output to this path. All substance lives in the instruction files (P2).
- Model selection per role is configurable (e.g. stronger model for reviewers, cheaper for workers).
- Capture stdout/stderr and the exit code per call. Record token usage where the agent exposes it.
- Research and decide how to drive OpenCode: plain CLI per worker versus its server/SDK (see section 14).
## 12. Configuration and CLI

Configuration: global config (agents, default models, limits, notification and viewer settings) plus per-target-repo config (base branch, branch naming, check commands, protected paths, plan directory). A standard format (TOML or YAML), validated at startup with clear errors.

Minimum commands:

- `orch new <slug>`: create `feature.md` from a template
- `orch plan <slug>`: start or continue the planning discussion with Codex
- `orch approve <slug>`: approve the plan (records file hashes)
- `orch run <slug>`: PREPARE through the next gate, optionally opening the viewer
- `orch next <slug>`: perform the correct next action for the current phase
- `orch watch [slug]`: live dashboard
- `orch pause <slug>` / `orch resume <slug>`
- `orch status [slug]`: all features and their phases, or one in detail
- `orch done <slug>` / `orch reopen <slug> <notes-file>`
- `orch abort <slug>`: terminate agents, keep state
- `orch clean <slug>`: remove worktrees and temp branches the run created
Notifications: on pass, on escalation, and on fatal error, write a human-readable `REPORT.md` and send a notification through a pluggable channel (desktop notification and webhook to start with; keep it simple).

## 13. Non-functional requirements

- Language: Python (propose alternatives if you have a strong reason).
- **Supported platforms (both are my development machines, both are first-class):**
  - **Fedora Linux**, native, with bash/zsh.
  - **Windows with PowerShell 7 (`pwsh`)**, native. WSL2 must not be required, and I launch coding agents from PowerShell 7 today.
- One codebase, no platform forks of the logic. Isolate platform differences behind small helpers: process launch, process-tree termination and timeouts, path handling (separators, long paths, case sensitivity), line endings, file locking, git worktree paths, terminal launching, and notifications.
- Use `pathlib` and argument lists rather than shell strings; do not rely on bash-only or PowerShell-only syntax. Check commands from the manifest run through a defined shell policy (documented per platform), since `pytest -q` style commands behave differently under `pwsh` and bash.
- Resolve agent CLIs (`codex`, `opencode`) from PATH on both platforms, including Windows shims (`.cmd`, `.ps1`), and give a clear error if one is missing.
- Provide an install and run guide for both platforms (e.g. a `pipx` or `uv` install, entry point `orch` working in `pwsh` and in Fedora's shell).
- Tell me about any platform limitation you cannot avoid, rather than silently degrading.
- Small and readable. A person should be able to read the whole codebase in an afternoon. No plugin framework or DSL.
- Deterministic and testable: stage logic unit-tested with fake agent adapters; one end-to-end test against a tiny sample repo using stubbed agents.
- Safe git behavior: never touch main, never force-push, never delete branches or worktrees it did not create, and never run destructive commands outside the run's own directories.
- Secrets are never written to logs, state, or handover documents.
- Cost visibility: record per-call duration and token usage where available, and include totals in the report.
## 14. Decisions I want your recommendation on

1. **Orchestrator form:** I want it to be a program that uses OpenCode as the runtime for workers and reviewers, not an LLM session coordinating itself. Confirm, or explain trade-offs if you disagree.
2. **OpenCode driving method:** CLI per worker versus server/SDK sessions, including what each means for live viewing and attaching.
3. **Review granularity:** one review pass over the combined result, per-worker reviews plus an integration review, or both.
4. **Doc review:** a separate standalone agent (my default) versus a section of the code reviewer's job.
5. **Reassignment semantics:** what a replacement worker receives (fresh session from handovers and findings only, per P3).
6. **Merge strategy:** how and when worker branches combine, and how conflicts are handled when scopes unexpectedly overlap.
7. **Markdown contract robustness:** how to keep front matter reliably machine-valid when agents write it (schema validation, self-check step, repair-retry), and whether the orchestrator should write or fix front matter itself.
8. **Live viewing:** the best terminal approach on Fedora and on native Windows with PowerShell 7 (see section 9), including whether one Python TUI dashboard can serve both.
11. **File locations:** confirm the split in section 5A (plan folder in the target project, run directory outside worktrees, worktrees for project changes only). In particular recommend defaults for `commit_plan`, `strip_plan_on_merge`, and `commit_run_history`, the default data directory on Fedora and on Windows, and how to give agents the absolute paths robustly on both platforms.
12. **Cross-worker dependencies:** how to implement merging or rebasing a dependency's branch into a dependent's worktree, and how to handle interface deviations flagged in handovers.
13. **Cross-platform verification:** how the test suite and the end-to-end sample run are exercised on both Fedora and Windows/PowerShell 7 (I will run them on my two machines; tell me what you need from me).
9. **Plan discussion mode:** how best to run the interactive requirement discussion with Codex (interactive session launched by the orchestrator versus the orchestrator relaying), keeping `discussion.md` current, and still producing the markdown artifacts non-interactively. Also whether and how to use Codex session resume as an optional bonus.
10. **Bootstrapping:** this tool cannot build itself with itself at first. Propose a milestone plan where the first usable version is built manually by you, and later changes run through the orchestrator.
## 15. Out of scope (for now)

- Web UI
- Multiple simultaneous features or runs
- Agents other than Codex and OpenCode
- Cloud or remote execution
- Automatic merge into main or any deployment
## 16. Acceptance criteria for v1

- I give a requirement; Codex plans with me and writes all deliverables as markdown; on approval, the orchestrator creates the branch and worktrees, runs workers in parallel, runs checks, runs the code and doc reviewers, loops on rework, and either reports a pass or escalates after the configured number of rounds.
- Every agent starts only from files produced by the previous stage and ends by writing its own output file; handover documents are present, validated, and sufficient for a fresh agent to continue.
- Workers update documentation; the doc reviewer catches a deliberately stale doc in a test scenario.
- `orch watch` shows live per-agent status, and works when attached to a run already in progress.
- A killed run resumes correctly, and I can stop and resume across separate sessions and machine restarts at every phase, including mid-discussion with Codex (via `discussion.md`) and mid-run.
- Nothing runs before `orch approve`; editing a plan file after approval is detected and requires re-approval.
- `step` and `auto` modes behave as specified; `orch status` shows every feature and its phase.
- I can start a feature from `feature.md` alone, without editing any orchestrator source code.
- A worker that edits outside its allowed paths, including the plan folder, is caught and sent back.
- Handovers land in the shared run directory, not in worktrees; a replacement worker and the reviewers read them from there, and a dependent worker can read the handover and branch of the worker it depends on.
- An edit I make to a plan file between stages reaches all agents without a restart, because they read the canonical plan folder by absolute path.
- Plan files are committed to the feature branch per `commit_plan`, and a feature can be continued from my other machine after a push and pull.
- A malformed handover or verdict is handled as specified.
- The full flow, including `orch watch`, is demonstrated end to end on a small sample repo on both Fedora and Windows with PowerShell 7, with no WSL2 involved on Windows.
- Process termination, timeouts, and worktree cleanup are verified on both platforms.
