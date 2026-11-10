# Dependency triage and repair ordering for multiple review findings

## Human direction

When a review produces multiple findings A–F, assess dependencies and determine a justified fix order before dispatching repairs. The coordinator schedules the owning workers from that assessment; it must not assume all findings are independent or simply use the verdict list order.

## Responsibilities

The originating reviewer proposes technical prerequisites and rationale for its findings as part of its evidence. The coordinator performs deterministic validation and scheduling, not unsupported semantic inference. When dependencies span code/docs roles, multiple verdicts or conflicting proposals, a separately assigned dependency assessor consolidates the graph. A fresh assessor need not run for a trivial, explicit, unambiguous graph. Specify this role, model preference, scope and budget in the future approved plan; do not silently use another paid review call.

Dependency assessment does not replace code correctness review, documentation relevance review, finding ownership or human risk/acceptance decisions. A docs-only reviewer must not inspect code to infer code dependencies. A code-capable assessor may inspect authorized technical evidence, while its structured output must remain code-free where supplied to docs-only review.

## Graph and fix order

Use stable issue IDs and owner identities. Distinguish:

- Semantic prerequisite: one fix depends on an interface, behavior or decision established by another.
- Integration/resource constraint: independent fixes touch the same owned paths or cannot safely share a mutable checkout.
- Verification dependency: implementation can proceed independently, but a check/review requires the assembled result.

Each semantic edge must name the prerequisite, dependent and evidence/rationale. Explicitly list independent findings rather than relying on omitted edges when evidence is incomplete. Validate known IDs, ownership, approved scopes and graph acyclicity. Missing information, cycles, contradictory dependencies or a required human decision block affected dispatch and request reassessment; do not guess an arbitrary order. Evaluate tightly coupled cyclic findings as a proposed bounded joint assignment, requiring approval where scope/budget changes.

Produce an ordered readiness plan, not a single mandatory serial list: prerequisites first, independent ready owners concurrently within the configured cap and disjoint-worktree/scope constraints, dependent implementation after committed prerequisite interfaces, and final verification when required assembled inputs exist. Findings assigned to one owner may be grouped where justified, retaining each issue's independent status, outcome and allowance. No concurrency change or new worker can be inferred without an approved dispatch plan.

## Per-issue progress and immutable evidence

Maintain individual pending/attempted/reassigned/finished/escalated progress and assignment evidence from the per-issue progress requirement. A blocked dependency is recorded as the reason an issue is not ready, not as a failed repair attempt. If A escalates, only graph-dependent work is held by A; independent work may continue within existing acceptance/snapshot gates. An unresolved blocking issue still prevents final promotion where required.

Keep review candidates immutable while reviewers use them. Starting a ready fix must not mutate another active reviewer's snapshot. Preserve candidate/approved-input binding and separate reviewer ownership. Dependency order does not authorize resolving another role's findings or falsely marking unprocessed issues finished.

Persist the triage graph, rationale, source verdict identities, plan version and dispatch/completion evidence in physical Markdown. Recompute readiness after accepted issue changes or approved graph revisions, without resetting counters or replaying completed dispatches. Approval-sensitive changes follow the existing approval rules.

## Acceptance scenarios

- A precedes B; C independent: A and C may run together, B waits for A's required committed interface.
- A escalates: B stays blocked without a charged attempt; independent C may finish; promotion still respects blocking A.
- Independent semantic fixes share an owned file: serialize or seek approved scope restructuring rather than concurrent conflicting edits.
- Code/docs prerequisites preserve role boundaries and use a qualified assessor for cross-role technical analysis.
- Cyclic, ambiguous and unknown-ID dependencies stop affected dispatch with an actionable explanation.
- Crash/resume preserves the graph, issue status and counters and avoids duplicate worker/reviewer calls.

## Delivery boundary

Future implementation requirement, to be reconciled with dependency-driven scheduling, planner-led escalation and per-issue progress. It does not authorize another repair assignment, live dispatch, architecture expansion or acceptance waiver for LIVE-REVIEW-003. Do not change current verdict schema without an approved compatible migration plan.
