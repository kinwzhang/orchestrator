# Dependency-driven scheduling and earliest eligible dispatch

Date: 2026-10-30
Suggested feature slug: `dependency-driven-scheduling`
Status: requirement recorded for a later planning session; no implementation or active-run change authorized by this document.

## 1. Purpose

Orch should start an agent job at the earliest point its actual prerequisites are satisfied. Independent worker jobs and independent reviewer jobs should run concurrently whenever resources permit. Do not serialize a whole workflow merely because the planner assigned one implementation worker, or because the user selected step mode.

Agents define semantic dependencies in approved plan artifacts. The deterministic orchestrator validates and executes those dependencies, observes artifact/check readiness and enforces resource limits. It must not guess code quality or infer hidden semantic dependencies through an LLM scheduling decision.

## 2. Observed problem

In the first self-hosted run, model-preferences-cli, the planner selected one worker and set the manifest concurrency limit to one. The existing review scheduler uses that same limit to serialize the code and docs reviewers. These reviewers have separate roles and no dependency on each other's verdict, so they could run concurrently after the candidate is ready.

This behavior follows the current hard cap but encodes an unnecessary bottleneck in the approved plan. Correct the planning defaults and scheduling model rather than silently overriding an explicitly configured cap. Worker count, dependency readiness, concurrency budget, step-mode boundaries and human gates are distinct concepts.

## 3. Required behavior

### Declared dependencies and eligibility

- Represent dependencies for worker and reviewer jobs in strictly validated approved Markdown contracts. The planner explains why a job depends on another and defines the exact artifacts/checks it consumes.
- Validate identifiers, referenced jobs, acyclic dependencies and satisfiable artifact requirements before execution. Reject missing or contradictory prerequisites with actionable errors.
- A job is eligible only when its declared dependencies have completed successfully and its required inputs have passed the deterministic validation applicable to those inputs.
- Once eligible, launch it as soon as capacity and the selected run mode permit. A completed upstream job should release its dependent without waiting for unrelated workers to finish an entire batch.
- Preserve existing dependency imports into worker worktrees. Completion that releases a dependent means validated source changes/handover and the required imported dependency baseline, not merely process exit zero.
- A prerequisite failure blocks its dependents, not unrelated eligible jobs by default. Define cancellation/escalation behavior explicitly for fatal run-wide failures, approval invalidation, user pause and abort.
- Use a stable deterministic tie-break order when several jobs become eligible simultaneously and capacity is limited. Do not starve a ready job by repeatedly launching later jobs.

### Independent reviewers

- By default, code and docs review do not depend on one another. Launch both concurrently when their shared candidate prerequisites are ready and the hard cap allows at least two jobs.
- Treat reviewer outputs independently: each role owns its findings and has its own verdict validation, repair, durable acceptance and rework routing. Do not combine verdicts into a single all-or-nothing resolution set, and do not let a docs relevance verdict resolve code findings or vice versa.
- Checkpoint and route each completed role result without waiting for another independent reviewer to finish. If one role's verdict is malformed, preserve the other role's valid result and retry only the invalid role for unchanged evidence. Resuming must not rerun or double-count an already accepted unchanged role result.
- Independent routing does not authorize mutating an immutable candidate while another reviewer is still reading it. Queue repairs or use a separately approved safe snapshot/cancellation policy. Promotion waits for both roles to pass the same final snapshot; that shared gate is not an output dependency between reviewers.
- One worker does not imply concurrency one. Planning guidance must not automatically copy the number of workers into a global agent cap.
- Preserve distinct review evidence: code reviewer inspects implementation against requirements/acceptance; docs reviewer reads only permitted code-free documentation and handovers for relevance.
- Both final verdicts must refer to the same immutable candidate SHA and approved input hashes. Earlier speculative review must not count as approval of a later candidate.
- Candidate assembly, required checks and source coverage remain review prerequisites. Do not launch final review against incomplete mutable changes solely to start earlier.
- If one reviewer truly consumes another job's output, express that dependency explicitly and explain it in the plan. Do not assume all reviewers must be parallel in every future workflow.

### Resource limits

- Keep a clear explicit hard cap on simultaneously active agent jobs across workers, reviewers, replacements and conflict resolvers. Scheduling must never exceed it.
- A user-selected cap of one intentionally serializes independent jobs and must be respected. If the manifest requests parallel review but its cap prevents it, expose that consequence during planning/status instead of silently presenting review as parallel.
- Preserve the currently supported maximum of four concurrent agent jobs unless a separately approved requirement changes it. A default cap of four allows parallel jobs without obliging the planner to create four workers.
- Separate role-specific limits only if planning demonstrates a concrete need. Do not introduce a queue service, plugin framework, scheduler DSL or speculative resource-allocation machinery.
- Preserve per-agent timeout, infrastructure retry, per-issue repair/reassignment and total-runtime budgets. Starting a replacement or resolver does not grant an extra slot outside the cap.

### Human gates and step mode

- Auto mode proceeds autonomously through eligible work, deterministic checks, independent reviews and bounded rework until plan approval, escalation or final human testing requires action.
- Do not invent a human confirmation between independent jobs merely because another job just finished. Preserve explicit operator pause/abort and already-required approval gates.
- Step mode remains an intentional operator stop at documented stage boundaries. Within an authorized stage, jobs follow their dependencies and capacity immediately; do not require a separate next command per ready agent.
- Do not cross a step boundary automatically without user authorization. If introducing job-level readiness changes existing stage boundaries, propose precise semantics during planning and update the runbook. Do not remove step mode or silently turn it into auto mode.

## 4. Rework, evidence and recovery

- Preserve stable logical job IDs, per-attempt IDs and issue lineage. A replacement continues the same task/scope/worktree in a fresh session, using its configured model/reasoning settings and preserved history.
- A changed dependency or input invalidates downstream completion/check/review evidence as appropriate. Do not reuse a verdict for a different candidate or approved input snapshot.
- Persist dispatch intents, eligibility/completion, imported dependency SHAs and outcomes using existing durable primitives. Recovery cannot rely on in-memory readiness queues.
- After a crash or reboot, reconstruct eligible jobs without redispatching valid completed jobs or duplicating possibly-live attempts. Retain fail-closed uncertain-process handling and the reviewed audited-recovery behavior.
- No dependent job may become runnable from a completed-but-uncheckpointed side effect until its evidence is reconciled correctly. Preserve pending operations and counters across restart.
- Keep physical Markdown checklist, activity log and issue register current. Clearly show pending/running/done/failed jobs, dependency blockers, capacity waits, step boundaries and human gates. Do not display an old reviewer attempt as if it were the current cycle's result without cycle/attempt context.

## 5. Operator visibility and planning output

The approved plan and manifest must make the job dependencies and concurrency policy understandable before approval. Show, in plain language:

- which jobs may overlap;
- which prerequisites block each job;
- the explicit total concurrency cap;
- the difference between worker count and active-agent capacity;
- when the next human action is actually required.

Status/watch should distinguish at least waiting for dependency, waiting for capacity, waiting at a step boundary and waiting for a human gate. Reuse existing logs/snapshots and keep the presentation concise; a new web UI is out of scope.

## 6. Acceptance scenarios

1. With one worker and a cap of at least two, both independent reviewers begin before either review completes, after the immutable candidate and required checks are ready. Verify actual overlap using stub synchronization barriers, not timing assumptions.
2. With cap one, reviews execute serially and status explains the capacity restriction. Never override the cap in the name of performance.
3. With independent workers A and B and worker C depending only on A, C starts after A's required checks/commit/import complete even while B is still running, provided capacity exists. This catches whole-ready-batch scheduling that waits for B unnecessarily.
4. A failed prerequisite blocks its dependent while unrelated eligible jobs follow the documented failure policy. A dependent never starts on a partial/blocked handover or failed check.
5. A configured reviewer dependency is honored; invalid references, cycles and impossible readiness conditions are rejected before calls.
6. A shared counter across real asynchronous stub jobs proves the hard cap is never exceeded, including replacement and conflict-resolver jobs. Stable tie-breaking and absence of starvation are exercised.
7. Step mode stops at stage boundaries while permitting concurrent eligible jobs inside the stage. Auto mode requires no per-job human prompts and stops at the existing human gates.
8. Crash fault injection before/after dispatch, job completion, dependency import and checkpoint verifies resume reconstructs readiness without duplicate jobs, lost completion or bypassed uncertain ownership.
9. Dependency rework invalidates affected downstream evidence; unaffected work is retained. Final code/docs pass must match the same SHA and approved snapshot.
10. Plan/config edits retain normal approval invalidation. Defaults encourage parallel review but explicit cap-one configurations remain supported under a documented migration policy.
11. Markdown records and status expose accurate current-cycle attempts, prerequisites and capacity waits. Missing/no-output logs do not make the dashboard stop or imply completion.
12. Independent reviewer output tests prove that a malformed docs verdict cannot discard or double-charge an accepted code verdict, each role resolves only its own issues, role completion is checkpointed promptly, resume retries only pending/invalid unchanged results, and different-candidate passes never authorize promotion.
13. Existing tests, new meaningful scheduler tests and Ruff pass. Native Fedora and Windows/PowerShell 7 results are reported separately; deterministic overlap tests use no paid/live calls.

## 7. Scope, delivery and limitations

Implement through a later reviewed orch feature, after the current model-preferences task is settled. The planner defines exact schema/CLI compatibility, module ownership, migrations, stage semantics and test interfaces. Prefer a small event-driven scheduler over additional infrastructure. Preserve standalone Python, local Git worktrees and Markdown contracts.

This requirement does not change the current run's manifest or concurrency setting, approve any plan, dispatch agents, alter main, reset budgets, or authorize hot-swapping the active runner. It does not require speculative early reviews or let the orchestrator judge code quality. Live operational acceptance is separate from deterministic stub tests.

Related deferred issues: LIVE-PLANNING-001 (human discussion before planning readiness) and LIVE-DISPATCH-001 (provider error diagnostics). They remain separately scoped unless a later approved plan deliberately combines them; this scheduling requirement does not silently authorize their implementation.
