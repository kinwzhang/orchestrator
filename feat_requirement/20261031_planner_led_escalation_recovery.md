# Planner-led recovery after reassignment exhaustion

## User instruction

When an issue remains open and escalates after reassignment, engage a planner only if it blocks the original requirement under the currently human-approved acceptance scope. The planner explains the situation, obtains needed human input and proposes a fix plan. After explicit plan approval, return to the normal working cycle. Nonblocking issues are parked in the physical follow-up checklist.

## Trigger and impact decision

Evaluate this route after the issue's reassigned assignment has exhausted its permitted work or otherwise escalated unresolved. Preserve existing immediate safety/depth escalation rules; this route must not authorize work beyond their limits automatically. Other escalation types retain their existing behavior unless separately approved.

Assess whether the issue prevents satisfying an explicit requirement or acceptance criterion. Provide the affected criterion, demonstrated impact, evidence, uncertainty and blocking rationale. Use the current human-approved scope, retaining any acceptance amendments and the original requirement history. Category/severity alone is insufficient: Security is not automatically blocking or nonblocking, and a Nice To Fix label cannot waive a binding criterion.

If blocking status cannot be established, retain the stop and request human classification. Do not silently park a potentially blocking issue or start another repair. This minimal classification clarification is not authorization to launch a full fix-planning session for a confirmed nonblocking issue.

## Blocking path

- Preserve the candidate, worktrees, verdicts, assignment lineage, counters and audit evidence. Stop automated repair/reassignment for the affected issue. Unrelated eligible work may proceed only within existing dependency, safety and snapshot gates.
- Engage the configured planner role through the existing adapter and model preferences. Planner engagement does not itself authorize implementation or paid calls outside the operator-authorized run policy.
- Supply a Markdown escalation brief: original requirement and current approved scope, issue/root/parent identity, assignment and repair history, failed approaches, current candidate, evidence, remaining defect, practical impact, exhausted budgets and uncertainties. Internal JSON is not an agent input.
- The planner explains why prior attempts did not complete the requirement and distinguishes implementation defects, missing evidence, acceptance choices and runner defects. Ask the human for decisions where intent, constraints, risk acceptance, evidence availability or recovery authority is unresolved. Do not declare readiness without the required discussion.
- Produce a bounded fix plan with candidate/baseline, permitted file scopes, worker/model choices, dependencies, verification, acceptance criteria, integration/recovery sequence and explicit stop conditions. Explain any proposed fresh allowance or exception; do not reset or silently replenish historical budgets.
- Pause at explicit human plan approval. Changed plan inputs invalidate approval normally. Approval must cover the concrete proposed work, any scoped allowance exception and any acceptance amendment.
- After approval, execute the normal worker, CHECK, independent code/docs review, candidate integration/promotion and human acceptance cycle. Preserve ownership and same-candidate/current-input evidence gates. Re-escalation does not automatically create another planning/repair allowance; honor the approved stop conditions.

## Nonblocking path

Park the issue in the physical follow-up checklist with stable identity, requirement linkage, evidence, impact, reason it does not block current scope, known limitations and suggested future work. Preserve existing escalation/review history. Mark the current-stage disposition explicitly as deferred, never technically fixed or verified. Do not dispatch another planner or worker for that issue.

Continue eligible current work through normal gates only after the nonblocking disposition is supported by the approved scope. If it requires weakening an acceptance criterion or accepting risk, obtain human approval first. The owning reviewer retains authority over its findings; docs cannot close code findings. An open follow-up must not be confused with an unresolved blocking issue by the promotion gate. Define this distinction compatibly in the implementation plan rather than deleting issues or bypassing review.

## Persistence and operator experience

Maintain physical checklist, activity log and issue evidence for classification, planner dispatch, human questions/answers, plan approval, parked follow-ups and resumed work. Make the current gate and next required human action clear in status/watch and command-return summaries. Persist planner attempt/process ownership and approval evidence for crash/reboot-safe reconciliation; repeated resume must not launch duplicate planners or repeat accepted accounting decisions.

## Acceptance scenarios

1. A blocking issue exhausts original and replacement assignments: stop repair, produce escalation brief, engage planner once, explain/ask, wait for explicit fix-plan approval, then run the approved working cycle.
2. A confirmed nonblocking issue at the same point: park with evidence and continue eligible work without planner invocation; do not claim fixed.
3. Uncertain impact or an unmet binding security criterion cannot be silently parked.
4. Human-approved risk/acceptance changes are recorded and reflected in fresh approval/review evidence.
5. Crash/resume at classification, planner dispatch, discussion and approval boundaries preserves history and avoids duplicate processes or allowance resets.
6. Root/derived lineage, depth stops, independent reviewer ownership and same-candidate promotion remain intact. A failed approved escalation plan stops at its defined boundary rather than looping indefinitely.

## Delivery boundary

Future implementation only. This document does not change current live state, grant an allowance, launch a planner, amend the active plan or supersede the separately authorized LIVE-REVIEW-003 repair assignment. Coordinate with issue-impact classification, planner-discussion gating, dependency-driven scheduling and the follow-up checklist requirements.
