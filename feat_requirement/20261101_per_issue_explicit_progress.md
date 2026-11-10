# Explicit per-issue progress and bounded dispatch stamps

## Human design direction

The operator rejects vague batch-level processed terminology. The coordinator must maintain progress for each issue independently. Its operator-facing vocabulary is pending (not dispatched), attempted 1/2/3, reassigned 1, finished, or escalated. No batch stamp may imply a disposition for issues never handled.

This direction addresses root LIVE-REVIEW-003 and requires planning reconciliation before implementation; it does not authorize another repair chain or replace independent review of integrity requirements.

## Required meaning

- Pending: the issue has not yet been dispatched for its current assignment. A discovered issue must have its own entry before dispatch.
- Attempted N: actual dispatch/attempt history for that issue's assignment, retaining N and its evidence. Dispatch count is not the count of reviewer invocations. An attempted stamp alone must not imply that execution has completed, passed review or is safe to retry after uncertain ownership; retain the corresponding activity and durable execution evidence separately.
- Reassigned 1: the single reassignment is recorded explicitly with original assignment history retained. The new assignment has its own attempt counters under existing approved rules; subsequent attempts do not erase reassignment history.
- Finished: the issue has the required owning-reviewer disposition under the current approved scope and candidate evidence, rather than merely a worker claiming completion. Where a human deferral satisfies current-stage acceptance, state that disposition explicitly instead of claiming the defect technically fixed.
- Escalated: further work on the issue is stopped pending the required human/planner disposition. Preserve the reason, exact boundary and all counts.

Use these stamps as clear per-issue progress, with assignment/reassignment counters, current activity and review evidence as explicit attributes rather than conflating all dimensions in one ambiguous processed flag. Do not add a new permissive lifecycle interpretation merely by renaming existing state.

## A–F example and interrupted batches

A review reports A unsuccessful and B–F resolved. If handling A escalates and the coordinator stops before handling B–F, A is explicitly Escalated and B–F retain their actual prior statuses. Keep Mary's resolution claims as pending-to-apply review evidence; do not mark them Finished, reset their assignment status to Pending, or infer their handling from a completed transaction marker. If B–F were individually validated and applied before the stop, their actual Finished statuses may be retained. The record must distinguish each case without guessing.

Every issue referenced by a review must have an explicit durable application disposition describing whether this review changed its stamp or left it untouched; this application evidence is distinct from an issue's worker dispatch status. Mechanical persistence completion must never stand for completion of all issue work.

On restart, resume from the retained per-issue dispositions and exact source review evidence without double-charging, re-dispatching uncertain attempts or overwriting history. Preserve the required candidate/approval/reviewer provenance checks. Simpler labels do not create missing historical proof or permission to fix old records.

## Budgets, scope and acceptance

Preserve existing limits (including stop after two unsuccessful repairs despite maximum three attempts), reassignment rules, issue lineage and human approval gates. The word attempted must not authorize a third unsuccessful repair or a fresh replacement. Reopened issues retain their prior finished assessment and superseding decision history under the approved once-per-repair rules.

Test each A–F disposition, early escalation, crash before/after each per-issue update, repeated review, uncertain dispatch and restart. Verify that displayed progress agrees with durable individual evidence, and that no batch marker falsely finishes untouched issues. Plan any schema/display changes explicitly and preserve legacy records without invented statuses or bindings.

No live-run mutations, additional allowances, disclosure integration, architecture expansion or activation is authorized by this document.


## Superseding human refinement: lifecycle, counters and review cursor

This section supersedes the earlier combined stamp vocabulary. The small issue lifecycle enum is pending, in_repair, finished, escalated. Attempts, unsuccessful outcomes, review rounds and reassignments remain separate historical counters with assignment attribution. Operator-facing substatus is derived, for example “in_repair · assignment original · attempt 2” or “in_repair · reassignment 1 · attempt 1”; it is not another independently writable lifecycle field. Preserve real activity information where needed, including running, awaiting check/review and uncertain ownership; a display label cannot authorize retrying an uncertain process.

Reassignment examples do not increase allowances. The existing one-reassignment limit remains binding; do not display or permit reassignments 2/3 as though authorized. Maximum attempt limits and the stop-after-two-unsuccessful rule remain distinct.

### Per-review application is a different dimension

Keep an issue's lifecycle separate from whether a particular review sheet has applied an entry. B–F untouched by an interrupted review keep their exact prior issue statuses and counters. Represent “no entry applied yet” explicitly in the review cursor/application map. This is review-entry pending, not a command to overwrite the issue status as pending. An issue already finished remains finished until an authorized accepted reassessment changes it.

If escalation on A interrupts application, atomically persist A's escalation together with exact remaining review-entry identities, source review/candidate/input binding, applied entry decisions and a deterministic resume marker. Alternatively use a crash-resumable durable intent/result protocol with equivalent guarantees. Do not mark the entire review fully applied while entries remain. Resume must not charge A twice or manufacture dispositions for B–F. Apply/validate each remaining entry before using it for dispatch: a review claiming B resolved is not an instruction to repair B blindly.

### Legal transitions and guards

The implementation plan must define a complete guarded transition table and reject unlisted transitions without partial mutation. Minimum normal transitions:

- pending -> in_repair when authorized repair work is actually dispatched.
- in_repair -> finished after required owner review confirms acceptance for the relevant work.
- in_repair -> escalated when the relevant stop condition is reached.
- escalated is terminal for automatic work on that issue until an explicit human disposition authorizes its defined continuation.

Planning must also specify necessary guarded cases rather than leaving them accidental: a human resolution or acceptance deferral, escalation before first dispatch, unchanged-state review application, new reassignment within in_repair, and valid reopening of finished work. Reopening must preserve prior review/accounting and follow the approved superseding-assessment rule. Human continuation is not a counter reset. Do not invent an unguarded finished -> pending or escalated -> in_repair shortcut.

### Independent work after escalation

Escalation is issue-scoped where safety and dependencies permit. A remains escalated awaiting human action. Continue applying pending entries for B–F, then dispatch only those whose accepted dispositions require repair and whose dependencies, ownership, current approval and concurrency limits make them ready. If B depends on A, B waits without consuming an attempt. Independent ready issues may continue; shared-path constraints may still serialize them.

Global integrity or provenance uncertainty may require a run-wide stop. Do not continue independent work when the review sheet itself, original bindings or safe application state are untrusted. Remaining blocking issues continue to prevent final promotion even if unrelated repairs finish.

### Acceptance additions

Test the transition table, invalid-transition refusal, derived display/counter consistency, and atomic/resumable stop at every A–F boundary. Show that B–F retain prior lifecycle statuses while entries are pending, resume applies remaining entries exactly once, independent repair dispatch can continue despite A's issue-scoped escalation, dependent issues wait, and a global evidence failure still stops unsafe work.

This is a revised planning requirement only. Existing source schema uses different statuses; do not rename live state, mutate counters or implement a migration without an approved compatible plan. It does not lift the current LIVE-REVIEW-003 architecture stop or authorize dispatch against corrupted/unbound historical evidence.
