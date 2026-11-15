# Round two: operator visibility and bounded OpenCode investigation

## Status and selected scope

Prepared for human review, not execution approval. On 2026-11-15 the operator selected status tables, status after next, system-local watch timestamps, and a noncritical OpenCode documentation-review configuration investigation. The backlog is discussion input, not blanket authorization. Do not approve this plan, dispatch implementation, or invoke live/paid agents during planning.

Priority and delivery order: (1) status tables and reusable renderer, (2) next-command reporting through that renderer, (3) local watch timestamps. The OpenCode investigation can proceed independently alongside this sequence. One presentation owner prevents foreseeable overlap in CLI, watch and presentation tests. Two workers, concurrency two, also leave independent code/docs reviewers able to run concurrently subject to existing scheduling.

## Baseline and retained work

Baseline feature/orch-round-2 and main: e5c79bbf55a17fe3e13d07eb3cc903515872bf82; verify main still matches before approval. The initial worktree had only untracked .plans artifacts. Current-run Markdown checklist/activity/issues contain a planning attempt, no implementation handovers and no issues. First-cycle accepted work is already consolidated; documentation/20261110_first_cycle_archive.md identifies historical evidence and retained branches. No historical branch is an integration input for this round. Do not import, reset, clean, overwrite or revive historical work or allowances. If an operator requests additional retained changes, inventory exact commits and overlap, then revise the plan before dispatch.

## Requirement mapping and acceptance

A1 — feat_requirement/20261110_status_table_layout.md. Status displays slug, phase, stage, cycle, next action, plan/run paths and prominent blocker/human action. Agent rows retain role/activity/status, attempt and attempt-relevant checks. Issue rows retain ID, lifecycle, assignment/reassignment/attempt details, owner, dependency/blocker information, summary, legacy disposition, root/parent context and finished-versus-deferred meaning. Review application gaps remain separate from lifecycle. Deterministic ordering, empty sections, long text, narrow/wide widths and plain redirected output must remain usable; move long details below tables rather than lose identity or blocker meaning. Status with no slug retains a compact multi-run overview, including interrupted creation entries. Rendering uses existing Rich and literal safe text; terminal escape or markup injection must not return. No state/journal/approval mutation, workflow advancement or provider call occurs.

A2 — feat_requirement/20261031_next_status_summary.md. After next completes a successful stage, reaches a human gate, or handles a failure/escalation, print exactly one current persisted status using the same renderer as status. Preserve explanatory messages and existing exit codes. Reload after handling/reconciliation, never advance an additional stage. Preserve the primary failure if reloading or rendering fails and emit a concise status-unavailable notice. Invalid arguments/unidentified runs need no summary. Exercise successful, gate, failure/escalation and secondary-render/load-failure cases using temporary runs/stub agents; verify no extra stage/dispatch and unchanged lifecycle/exit behavior.

A3 — feat_requirement/20261031_watch_local_timezone.md. Last activity is labeled local and converts aware instants using the system zone and instant-specific numeric UTC offset, including supported daylight transitions. Readable date/time includes seconds and colon-separated offset. Never hardcode the launch environment timezone. Absent values display an em dash; malformed/naive legacy values display unavailable (unverified timestamp), without assuming UTC or echoing unsafe input. Persisted UTC timestamps, ordering and duration arithmetic remain unchanged. Test UTC, non-UTC, offset transition, missing/malformed/naive values and immutable inputs. Use portable conversion seams and deterministic fixtures; separately record native Fedora and Windows evidence. Timezone changes during an already-running dashboard are out of scope.

A4 — backlog item 13, noncritical investigation. Identify the later-loaded OpenCode configuration and loading order with sanitized provenance, installed CLI version and minimal reproduction. Distinguish inherited settings, CLI loading behavior and conservative validator behavior; do not call this a permission bypass without evidence. Compare an isolated temporary configuration with synthetic inherited allow configurations. Prove approved code-free Markdown can be read, source access denied, verdict written only to its exact approved path, and unapproved reads/writes/tools remain denied, using deterministic tests and the local fake provider only. No paid/provider-network calls, real credential access or user configuration edits. If environment/version evidence cannot safely be obtained, record the limitation and reproducible follow-up instead of claiming a diagnosis. Deliver a code-free investigation report even if no fix is warranted. Production validator/policy changes are excluded and require a separately reviewed scope proposal. An unresolved environment-specific reproduction is not a blocker for A1–A3; unsafe actual behavior is a finding and must be escalated. Never waive a required deterministic check silently.

## Interfaces and ownership

W04 owns src/orch/cli.py, src/orch/watch.py, src/orch/reporting.py, tests/workflow/test_presentation.py, tests/e2e/test_round2_presentation.py and documentation/20261115_operator_visibility.md. The shared status renderer accepts an already-loaded run snapshot, the plan and run paths, a terminal width defaulting to 100 columns, and a choice between detailed output and a compact overview. It returns plain terminal-safe text without reading, writing or mutating anything. It derives the next action with the existing workflow helper and uses existing Rich rendering to a buffer. CLI owns console-width selection and loading. Status selects detailed output when a slug is supplied; next always requests detailed output. CLI handles persisted reload and error preservation outside the renderer; durable storage and workflow semantics remain unchanged.

Watch owns a local-activity formatter that accepts a stored activity value and returns display text. It validates timezone awareness before converting with the system-local timezone applicable to that instant. The existing watch table entry point continues to accept a state store and run identifier. Tests may control the conversion boundary without a Unix-only dependency. No new persisted fields, contract migration, dependency or package changes.

The following exact technical signatures are implementation context only. The existing runner omits this fenced block from documentation-review copies; the preceding prose fully describes the interface behavior needed for relevance review.

```python
# In reporting.py:
render_status(state, *, plan_path, run_path, width=100, detail=True) -> str
# In watch.py:
format_local_activity(value) -> str
# Preserve the existing watch entry point:
status_table(store, run_id)
```

W03 owns only tests/agents/test_round2_docs_configuration.py and documentation/20261115_opencode_docs_configuration_investigation.md. Read agents.py, existing agent tests and docs-role preparation paths to investigate, but do not edit production adapters, shared tests/fixtures, real settings or permission policy. Synthetic configurations and fake-provider fixtures belong in its new test file. The existing integration test for documentation-review source denial and verdict writing remains authoritative; new tests must not monkeypatch away its policy checks. Report any required production fix for separate approval.

## Overlap and dependency matrix

| Work | Files/interfaces potentially shared | Owner / resolution | Dependency |
|---|---|---|---|
| Tables + next summary | cli/reporting and presentation fixtures | W04 handles both sequentially within one assignment | renderer before next integration |
| Local time + tables | watch and presentation tests | W04 owns all edits | independent internal formatting work |
| OpenCode config | agents policy and role input preparation | W03 reads only; dedicated new test/report | none on W04 |
| Documentation | two distinct new code-free Markdown files | respective worker owns its file | report follows its own evidence |
| Historical lifecycle/model preferences | accepted baseline behavior | regression only; no historical branch import | prerequisite baseline confirmation |

Workers have disjoint manifest scopes and no cross-worker dependencies or shared mutable interfaces. Shared fixture/dependency needs go to the coordinator/W01 for plan amendment, not opportunistic edits. Unexpected conflicts require a separate bounded coding assignment with competing commits and expected combined behavior; escalate unclear intent. Integrator never silently repairs source.

## Checks and delivery gates

Before dispatch confirm baseline, clean owned scopes, dependency-free graph, exact path ownership and approval hashes. Run full pytest and Ruff on the immutable integration candidate. The OpenCode local fake-provider integration test may require local loopback/installed CLI; record skip/failure explicitly and distinguish environment limitation from defect. A known failure is not converted into a pass: review its evidence and seek an explicit operator check disposition/amendment if still blocking. No automatic xfail, deletion or assertion weakening. Missing native Windows evidence is reported separately; no Linux-only claim of cross-platform verification. Historical Fedora-first acceptance is context, not an automatic new waiver of defects or mandatory checks.

Both code and docs verdicts must pass for the same candidate before feature promotion. Code reviewer verifies all technical requirements, tests, containment and documentation accuracy against implementation. The existing runner copies the canonical requirement, this plan, documentation-review instructions, issue register, worker instructions, worker handovers and their declared Markdown documentation into the documentation-review directory. It strips fenced code blocks only; it does not remove inline code or automatically rewrite technical prose. Consequently, every copied planning document must remain code-free outside fenced blocks. Source paths and contract field names may identify context, but signatures, source excerpts and configuration examples must be fenced. Workers must keep their documentation and handover prose code-free as already required. No runner change is requested.

After fence stripping, A1–A4, ownership, delivery order, prose interface behavior, documentation deliverables and acceptance limitations remain available to the docs reviewer. It judges whether the operator guide and investigation report address those assignments clearly and relevantly; it does not verify implementation or technical containment. The code reviewer retains all technical verification. Exact permitted read/write paths remain enforced by the existing runner. Missing safe context or unexpected code in a supplied copy is an input problem, not permission to broaden access.

Promotion targets feature/orch-round-2 only. No main merge, push, deployment or destructive cleanup. Operator performs final usability acceptance. Preserve all counters/lineage; three repair attempts maximum but stop after two unsuccessful repairs, three review rounds per assignment, one reassignment, depth maximum two and depth-three root escalation immediately without repair. Charge each actual unsuccessful repair outcome once; unchanged approval revalidation consumes no new repair/replacement allowance. Replacement is a fresh session inheriting effective coder model with assignment allowances under existing rules, preserving predecessor history.

## Deferred scope

Scheduling/job graph expansion, finding classification/impact, fuller finding assessor/triage workflow, planner-led escalation recovery, planner conversation lifecycle, provider diagnostics, numeric YAML location fixes and native Windows certification are separate follow-ups. Per-issue lifecycle/model preferences are delivered baseline regressions, not reopened tasks. Pre-dispatch overlap resolution is a planning discipline applied here, not a new automated enforcement feature.

## Verification record

Planning checks and baseline outcomes are recorded in discussion.md. Baseline full suite: 536 passed, 7 skipped; Ruff passed. The named OpenCode fake-provider reproduction passed separately (1 passed); the previously reported failure is not reproduced here and its cause remains an investigation question, not an established defect. Approval is a later explicit operator action even when discussion.ready is true. Workers write named handovers only to runner-supplied external run paths; no notes in the worktree.
