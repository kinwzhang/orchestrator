# Independent reviewer roles, ownership and review-batch recovery

This note defines the repaired code/docs review contract. It replaces the old
combined verdict routing, which could resolve code-owned findings from a docs
relevance verdict and could partially charge counters before rejecting the
conflict. Review acceptance now also applies each verdict entry individually and
durably, so an issue-scoped stop in one entry cannot silently complete or discard
the rest of the batch. The repair is scoped to review routing, issue ownership,
per-role transactions, per-entry application and the audited recovery command. It
does not change planning, candidate assembly, worker budgets or the hard
concurrency cap.

## Roles, ownership and authority

Code and documentation review are independent roles. Each has its own input
scope, owned findings, verdict validation, durable result and routing
transaction. Neither role waits for the other's verdict, and neither may resolve
or reopen the other's findings.

Ownership is recorded as `owner_role` on every accepted issue:

- `code` — opened by an accepted code-review finding.
- `docs` — opened by an accepted documentation-review finding.
- `system` — created by the workflow (`check-<worker>`,
  `post-check-<worker>`, `integration-check-<worker>`). Reviewers cannot resolve
  these.

A role's verdict may report findings only against issues it owns or brand-new
issue IDs. It may resolve only existing issues it owns. A code pass must
explicitly resolve every open code-owned issue; it does not need to mention
docs- or system-owned issues. A docs pass works the same way for docs-owned
issues. Issue-name prefixes never grant authority.

Legacy issues with no recorded owner are migrated once, before review
validation, from retained verdict evidence:

- workflow-generated check IDs are recognised by their exact generated identity;
- any other issue must be declared as a **finding** by exactly one reviewer role
  in a retained `round-*/verdict-*.md` file;
- appearing only in another role's `resolved` list does not establish ownership;
- missing or ambiguous evidence refuses migration and escalates the run.

`issue_ownership_resolved` records each migrated decision once. Migration
validates every pending issue before changing any owner, so a refused entry
leaves the whole register untouched. The audit event is written before the
ownership state so an interrupted migration can finish on re-entry without
losing the resolution note or writing it twice. Retained audit for one issue must
name either no role or exactly one role. Multiple distinct roles are a conflict
regardless of journal order; repeating the same role is accepted under the
replay policy. Ordinary migration, audited recovery and recovery replay validate
the complete journal and every proposed entry before appending or applying
anything, so a conflict cannot leave earlier ownership changes partially
applied.

Platform evidence follows the same authority boundary. The orchestrator does not
infer whether a native platform is available and never marks unverified evidence
passed. A reviewer that needs evidence the current environment cannot produce
(and that no same-platform repair can supply) uses `action: escalate` and names
the exact human prerequisite. That escalates the issue instead of spending the
remaining repair budget on repeated attempts that cannot succeed. Changing the
approved acceptance requirement still requires human approval.

## Verdict and evidence binding

New verdicts include `reviewer`, `result`, `findings`, `resolved` and
`candidate_sha: <immutable candidate>`. `candidate_sha` is optional in the
schema so historical verdicts remain readable; live acceptance requires it to
equal the candidate being reviewed. Historical (legacy) acceptance is allowed
only through the audited recovery path, which verifies the recorded round
transition, dispatch intent, completion and attempt identity instead, and
records the result with `binding: legacy-attempt`.

The review-evidence digest covers the candidate SHA, all approved input hashes,
the reviewer instruction hash and every role input hash except the rendered
issue register. The register is a projection of routing state that a role's own
acceptance mutates; excluding it keeps an accepted result reusable on resume and
avoids self-invalidation.

## Repair-outcome versus review-round accounting

The issue counters answer different questions and are charged separately:

- `repairs` counts worker dispatch attempts for the issue in the current assignment.
- `assessed_repairs` is the highest repair ordinal whose outcome a reviewer has
  already judged.
- `failed_repairs` counts distinct unsuccessful repairs, at most once per repair
  ordinal. The additive `failed_repair_ordinals` provenance lists those exact
  positive ordinals; a missing value is legacy state and is reconstructed only
  from retained transactions when attribution is unambiguous.
- `review_rounds` counts accepted assessments of distinct work outcomes. A failed
  repair or a newly resolved issue advances it once; repeated review of the same
  outcome, including initially unrepaired work, does not advance it again.

An approval-only change (for example a temporary runtime-limit extension)
invalidates the approved-input digest and forces a fresh reviewer run and a new
role-result transaction. It does not create a worker repair. If the candidate and
the repair outcome are unchanged, the new verdict is validated and accepted
against the current inputs, but the failed-repair and review-round counters stay
put and no replacement is created. A genuinely new unsuccessful repair advances
`repairs`; the next assessment charges `failed_repairs` once and records the new
high-water mark, so the configured stopping and one-reassignment policy still
applies. Replay of a partially committed transaction recomputes the same plan
from the recorded base register and is therefore idempotent.

A valid finding that reopens an issue previously resolved at the same repair
ordinal supersedes that premature successful assessment. It charges that exact
assignment and repair ordinal once as unsuccessful, preserves the earlier pass,
and records a `superseding_failure` accounting decision. It does not increment
`repairs` or consume another review round. Repeated reopening of that same
outcome records `already_charged` and cannot create another charge. If the new
charge reaches the configured failure or reassignment boundary, the normal
stopping policy applies. A later actual repair is assessed against its own
ordinal and assignment.

Accounting decisions are persisted in each new review transaction and acceptance
audit event, including the issue, assignment index, repair ordinal, assessment
kind and whether a failure/review-round counter changed. Replay verifies the
decision against the recorded pre-routing register. Routing and evidence
reconstruction use the same decision model; retained findings alone never imply
a new charge. The optional `failed_repair_ordinals` field is an operational
index of those durable decisions, not a replacement for their audit history.

The provenance fields are additive and optional for schema-v1 state. Routing
derives a missing assessment mark and exact charge ordinals from completed
review transactions: each transaction retains the complete pre-routing register
and accepted verdict. Ordinal zero is initial work before any repair; a finding
there records an assessment of initial work and can establish a mark of zero,
but it never counts as an unsuccessful repair. The derived mark and exact charge
set must agree with counters. A legacy reopening is reconciled once only when a
retained successful assessment, same assignment and repair ordinal establish
unambiguous attribution; missing or contradictory history refuses before the
next transaction is written.
Counters alone never prove an assessment: a history with no completed review
evidence, a duplicate charge for the same repair, an understated failure count,
or explicit provenance that disagrees with retained evidence is refused with an
actionable error. Legacy counters are preserved; the run is not silently granted
or charged another allowance. Audit the retained transactions and use an
approved recovery when the evidence is contradictory.

## Validation and transactions

A role verdict is fully validated before anything is written:

- reviewer identity and candidate binding;
- finding workers and issue lineage (parent/root/depth);
- ownership of every reported and resolved issue;
- no issue both reported and resolved;
- pass completeness for that role only;
- repair/review/reassignment budgets and depth limits.

Only then is a durable operation intent written. The intent contains the exact
normalized verdict, the complete pre-routing issue register, the ordered entry plan
with each entry's stable identity and deterministic proposed decision, and any
issue-scoped escalations. A durable review application is then persisted from that
intent. Entries are applied in verdict finding order followed by resolution order:
an entry's issue effects and accounting decision are saved atomically with its
applied decision, its audit event is recorded under the stable application and entry
identity, and an issue-scoped stop is recorded on the affected entry without removing
later entries. Completion is refused until every entry is applied and every audit
agrees. The role's `review_results` entry and acceptance event are recorded after the
entries, and the operation is marked completed last. Re-entry replays the recorded
intent and applies only decisions already persisted; it never recomputes from current
counters. A second, different verdict for the same role, candidate and evidence
digest is refused as a conflict rather than applied. A resolution entry never becomes
a repair request.

The operation key is
`review-result:<role>:<candidate-sha>:<evidence-digest>`. Completed results for
an earlier candidate remain historical evidence but never authorize the current
candidate. An interrupted intent for the current candidate is completed before
dispatch; an intent for another candidate blocks resume instead of being applied
silently.

### Per-entry application and unfinished batches

A verdict's entries are applied individually. An entry may legitimately escalate
(allowance exhaustion, depth stop, explicit escalation or a reassignment refusal)
and return before later entries receive decisions. When entry A stops, untouched
entries B–F retain their exact prior statuses and counters and remain not applied.
They are not treated as resolved, and the review transaction is not described as
complete. The remaining entries are validated and committed individually, and any
unfinished application is resumed before relevant dispatch.

Application completion means every entry has a validated disposition, not that the
issue finished, the review passed or the candidate may be promoted. Gap checks name
a missing application, unapplied entries, a missing completion record, missing or
duplicate entry audits and a non-verified evidence classification. Issue-scoped
escalation blocks promotion for the affected issue while independent eligible work
may proceed.

A no-change entry is still an applied entry with an explicit decision and invents no
charge. Each application retains the role and selected attempt, candidate, approved
inputs, verdict identity and digest, immutable pre-application evidence, entry order
and the per-entry decision map. The evidence authority requires the prerequisites
appropriate to the consumer; conflicting evidence stops the affected application,
and a missing or unvalidated prerequisite refuses authoritative application.

Each completed role is checkpointed immediately. Rework is queued only after
both roles have reached a terminal result for the same candidate, so worker
worktrees are never mutated while another reviewer is still reading the
candidate. A worker invalidated only because a dependency was reworked receives
the upstream finding, its verdict evidence and the dependency chain in its
feedback; no affected worker is dispatched with an empty feedback body. Promotion requires both accepted results to be `pass` and bound to
the same immutable candidate SHA. A missing or invalid role result leaves the
other role's accepted result intact, and resume retries only the incomplete
role for unchanged evidence.

## Legacy partially applied batch recovery

The run `model-preferences-cli-5ec7f5ee` exposed the old defect: the code verdict
requested rework on `code-w01-02`, `code-w01-03` and new `code-w01-05`, while the
docs pass listed `code-w01-01` through `code-w01-04` as resolved. The old router
charged the code findings' counters, created `code-w01-05`, then raised
`Issue is simultaneously resolved and open` before applying any resolution.
`code-w01-01` and `code-w01-04` therefore remained open even though the code
reviewer resolved them.

`orch recover-review` repairs exactly that shape. It is not a counter reset and
does not edit `state.json`, delete verdicts or fabricate events. It:

1. Requires phase `ESCALATED`, stage `REVIEW` and the exact conflict error.
2. Verifies the current approved input hashes, the immutable candidate HEAD and a
   clean candidate worktree.
3. Verifies each round verdict was produced by the recorded attempt: round
   transition, one dispatch intent, one completion, matching output path and
   attempt identity. A relocated/reviewed copy may retain the original absolute
   path; the run ID plus relative path must match.
4. Migrates legacy ownership from retained verdict findings and rejects any
   ambiguity.
5. Confirms the docs verdict is invalid specifically because it resolves
   non-docs-owned findings, and confirms the code verdict is valid.
6. Reconstructs the pre-batch register by inverting only the rejected batch's
   known mutations: one review round and, where a repair had been charged, one
   failed repair. The assessment mark is reconstructed from completed review
   transactions for the pre-batch counters; if that evidence is missing,
   understated or duplicated, recovery refuses before any write instead of
   inheriting the post-batch mark. Findings absent from the pre-batch snapshot
   were created by the batch and are removed.
7. Requires the reconstruction to equal, byte for byte, the independent
   pre-batch register copy written before that routing call
   (`round-<n>/docs-review/*-issues.md`), and requires that copy to predate the
   escalation event. Any mismatch refuses recovery.
8. Records an operator/reason operation intent, restores the validated pre-batch
   values, accepts the code role through the normal transaction and records the
   docs verdict as rejected without touching docs findings or counters.
9. Emits one `review_batch_recovered` audit event and completes the operation.
   Re-entry uses the recorded decision and evidence, ignores changed arguments
   and performs no second decision.

The docs verdict file is not rewritten. A later docs retry in the same round
writes an attempt-specific filename; review verdicts are never overwritten by a
later attempt. Original findings, verdicts and historical evidence stay in
place.

### Operator sequence for the observed run

Run from the reviewed, activated runner with the run stopped. Before acting,
recheck the values below; stop if any differs.

1. Back up the run directory and its worktrees together.
2. Confirm `state.json` is `ESCALATED`/`REVIEW`, the error is
   `Issue is simultaneously resolved and open`, `candidate.sha` is
   `4f134f315b9ab85e2058452b9895946f579355bd`, and the candidate worktree is
   clean at that SHA.
3. Confirm `round-2/verdict-code.md` requests rework on `code-w01-02`,
   `code-w01-03`, `code-w01-05` and resolves `code-w01-01`, `code-w01-04`; confirm
   `round-2/verdict-docs.md` passes but resolves `code-w01-01` through
   `code-w01-04`.
4. Run the dry run and read the reported restored/removed IDs, invalid resolved
   IDs and snapshot path:

   ```text
   orch recover-review model-preferences-cli \
     --operator <name> --reason "<what you independently verified>" --dry-run
   ```

5. If the dry run is exactly the expected batch, run the same command without
   `--dry-run`. Expected: `code-w01-01` and `code-w01-04` resolved;
   `code-w01-02` and `code-w01-03` open with one review round and one failed
   repair; `code-w01-05` open and code-owned; one accepted code result; no docs
   result; one `review_result_rejected` and one `review_batch_recovered` event;
   attempt 6 and the worker/infrastructure counters unchanged.
6. Start the resumed run with `orch resume model-preferences-cli`. The accepted
   code result is reused; only the docs role is dispatched again against the same
   candidate. Because the code result is `rework`, the next step is bounded
   worker rework, not promotion. The separate open code findings
   (`code-w01-02` native Windows evidence, `code-w01-03` sanitisation,
   `code-w01-05` flag scope) still require real repairs or a human prerequisite.
   If native Windows evidence cannot be arranged, pause before worker rework and
   treat `code-w01-02` as a human prerequisite; do not let repeated Linux
   attempts exhaust the issue budget. The preserved round-2 verdict says rework,
   and recovery must not rewrite it.

Do not re-run recovery with different arguments after a successful completion;
it returns the recorded result. Do not delete the rejected docs verdict, edit
the pre-batch register copy, reset counters, or hand-edit the journal/state. If
the helper refuses, preserve the run and review the evidence.

## Compatibility and limits

- Newly created runs use state schema version two; version-one state still loads
  and renders without being rewritten. `owner_role`, `review_results`, the
  per-assignment repair-assessment mark, activity records, transition history,
  dependency records and review applications are retained contract fields.
- Operational use of version-one records requires an explicit compatible migration.
  A version-one `open` issue with repair activity is ambiguous and refused
  operationally; it is never inferred as `pending` or `in_repair` from counters.
  Pure routing planning may project such a record only to compute a proposal.
- Older verdicts without `candidate_sha` remain parseable and are accepted only
  through the audited legacy recovery binding above.
- Review verdict paths are attempt-specific for new runs. Historical
  `round-<n>/verdict-<role>.md` files remain authoritative for legacy evidence.
- The hard concurrency cap is unchanged: both reviewers may run only when the
  configured cap allows two; a cap of one still serialises them.
- Approval invalidation, process-ownership recovery, uncertain-launch handling,
  infrastructure retries and per-issue budgets are unchanged.
- The repair and its tests ran on Linux only. Native Fedora terminal behaviour,
  Windows/PowerShell 7 process handling, ACL/reparse-point behaviour and locking
  are not verified by this work. The live run was inspected read-only; recovery
  was tested on a temporary copy and was not executed against the live run.
