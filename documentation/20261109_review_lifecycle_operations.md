# Review lifecycle operations runbook

- Date: 2026-11-09
- Status: operator runbook for the committed review-lifecycle contract
- Contract interface: `outputs/w01/1-interface.md`, source
  `7c78caec5b57ea2e441225f863febce034904548`
- Reducer interface: `outputs/w02/1-interface.md`, source
  `e9ea61ffd7529c5f2ad630567366b2cb56b78aae`
- Documentation baseline: assembled W01+W02 source `817f518d00129c05900bd50b397af1b7f69a8565`
  (integration `0c9d72cc0fc4b3420393d111f087d43a8f99a5f3`)
- Whole-chain verification: observed on assembled integration
  `8ad1901bbc49d343f3633220ac78723c0264907e` (W03 source
  `4e6df8ae6872d332a5809cb5d589450097a5934c`, W03 documentation
  `eb41010511d503bcf55cb91b97336db9b770f959`): full local suite 370 passed,
  5 skipped, lint and whitespace clean. Coverage limits and unverified platform
  gates are recorded in section 11. No Fedora acceptance or live operation is
  claimed.

This runbook is for an operator or on-call reviewer who must read a stopped or
escalated run, choose a guarded transition, continue work safely, or assess a
carry-forward inventory. It assumes the normative account in
`20261109_review_lifecycle_application.md`. Where an action needs a human control
surface, this runbook names the contract surface that exists; it does not invent a
command that does not exist.

## 1. Read the run before acting

The first pass is read-only. Stop the runner and back up the run directory and its
worktrees together before inspecting or changing anything.

1. Record the run's phase, stage and round, the reviewed candidate identity and the
   approved-input state. Recheck them from this run; never reuse another run's
   values.
2. For the issue in question, record the issue identity, its root and parent
   identity, its worker, the assignment index and identity, the repair ordinal, the
   owner role and the current lifecycle status.
3. Read three things separately: counters and assignment history; current activity;
   and the review application records. Do not infer a lifecycle status from
   counters. Do not infer a completed application from a refreshed Markdown mirror.
4. Classify each required piece of evidence as verified, recomputed unbound,
   unverifiable for missing inputs, or conflicting. Keep missing digests separate
   from missing inputs.
5. Confirm that no unfinished review application and no uncertain process ownership
   blocks the action you intend. Stop on any conflict or ambiguity.
6. Confirm the issue's recorded acceptance or deferral evidence, if any. A deferral
   is deferred for its recorded stage; it is not proof of a technical fix.

Useful rendered evidence includes the checklist's phase, stage, round and next
action; the activity log's ordered events; the issue register's lifecycle value,
optional legacy value and retained repair outcomes; and the status and report views'
application progress and dependency waits. State and the event journal are
authoritative; the Markdown mirrors are regenerated views.

## 2. Pre-action checklist

Before any transition, write down and confirm:

- the exact issue identity and the exact proposed source and target statuses;
- which listed transition is being used, and each guard fact that transition
  requires;
- the retained evidence for every guard fact, by event or record identity;
- that the source status is canonical, not an ambiguous legacy `open` record;
- that the action does not require an allowance the issue no longer has;
- that dependencies, shared resources and verification prerequisites are satisfied
  for the action;
- that process ownership is known for any issue about to be redispatched;
- that the review application, if one is involved, is reconciled and has no
  unapplied entry or duplicated audit.

If any item cannot be confirmed, stop. A refusal is a reason to inspect, not an
obstacle to bypass.

## 3. Apply a guarded transition

The procedure is the same for every status change. Most transitions are applied by
the workflow as it dispatches and accepts work; the explicit human surfaces are the
disposition path in section 6. Either way, the operator confirms the guard and the
result rather than editing status directly.

1. Identify the exact issue and the proposed source and target statuses.
2. Confirm that the transition is one of the listed transitions in the normative
   account. An unlisted transition cannot be applied at all.
3. Confirm every guard fact for that transition from retained evidence, not from
   counters or another run's values.
4. Apply only after validation. The transition authority refuses an unlisted or
   ungated transition before mutation.
5. Confirm the resulting status and the appended transition history.
6. Confirm unrelated counters, assignments, review rounds and outcomes are
   unchanged. A status change never edits counters.
7. Confirm any audit event recorded for the change.

Scenario notes:

- Registering a new issue establishes identity, owner and lineage and never implies
  dispatch.
- Dispatch requires a durable dispatch intent, ready dependencies, available
  allowance and known process ownership. It is the normal `pending → in_repair`
  entry.
- Acceptance finishes an issue only with the owning reviewer's acceptance for the
  relevant candidate, inputs and outcome. Acceptance of undispatched work is
  refused; it is not fabricated.
- Reopening requires exact historical attribution and remaining authorized work. A
  stale review cannot close newer work.
- Scoped human acceptance or deferral must record its scope, the required owner
  disposition and evidence. A deferral must name the approved stage.
- A no-change entry is still an applied entry and must not invent charges.
- Workflow-owned check issues are finished only by the workflow from deterministic
  check evidence.

## 4. Escalation with an unfinished review batch

A review verdict can contain several entries, conventionally considered as entries
A through F. Entry A may legitimately escalate and return before entries B–F receive
decisions. This is a supported outcome, not a failed transaction.

1. Confirm which entry escalated and on which issue.
2. Confirm every untouched entry's exact prior status and counters from the
   pre-application evidence and the retained issue register.
3. Treat untouched entries as not applied. Do not describe the review transaction
   as complete, and do not say the review passed.
4. Confirm the application record shows the applied entries, the derived next-entry
   position and an incomplete completion flag.
5. Resume the application so the remaining entries are applied individually from
   their deterministic decisions, validating each. Later entries are not removed
   because an earlier entry stopped.
6. Confirm that issue-scoped escalation blocks promotion for the affected issue
   while independent eligible work may proceed.
7. Resume unfinished application before any relevant dispatch.

A resolution entry is never turned into a repair request. An escalated issue is not
released by an ordinary resolution or finding; it needs a human disposition.

### 4.1 Promotion gates

Promotion waits until every gate holds:

- both reviewer role results are accepted, passing and bound to the same immutable
  candidate;
- every review application is complete, with every entry applied and every audit
  agreeing;
- no issue is unfinished or escalated without an explicit human disposition;
- dependency checks report no unresolved blocking issue or missing verification
  prerequisite;
- the integration record shows complete source coverage and the exact reviewed head.

A refusal names the exact blockers. Do not promote around an application gap, an
escalated issue or an incomplete review batch.

## 5. Replay and reconciliation

Replay reconciles a durable application from its recorded intent. It applies only
decisions that were already persisted, and it launches no worker.

1. Read the recorded application intent: entry identities, order, proposed
   decisions, candidate, approved inputs and the pre-application issue evidence.
2. Read each applied entry's decision and its audit event.
3. If an audit event is missing while the durable decision exists, reconcile it
   from that decision. If the audit event is duplicated, belongs to an entry that is
   not applied, or disagrees in digest, stop the application.
4. If the transaction is incomplete, resume from the recorded base and the
   remaining entries. Never recompute a decision from current counters.
5. If entries and audits agree, complete the application and confirm the completion
   record.
6. Re-entry after completion returns the recorded result; it performs no second
   application and creates no new decision.

Do not hand-edit state, delete evidence, fabricate an event or journal entry, or
rewrite a verdict. A contradiction is preserved and escalated, not repaired by
choice.

## 6. Human continuation, scoped acceptance and deferral

The only workflow surface for an explicit human control action is the contract and
Engine human-disposition surface, `Engine.record_human_disposition`, invoked on
behalf of a recorded human decision. It records the transition and a
`human_disposition` event. No CLI command for continuation or scoped
acceptance/deferral exists in this delivery; if a command surface is required, that
is a separate dispatch question and must not be improvised.

### 6.1 Continuation procedure

1. Confirm the issue is `escalated` and identify the exact scope to continue.
2. Establish explicit human authority covering the scope, dependencies and
   allowance disposition. Record the decision and its evidence.
3. Apply the `escalated → pending` transition with the continuation guard facts.
   Counters are unchanged.
4. Recheck dependencies, available allowance and process ownership.
5. Dispatch only through the normal `pending → in_repair` guard. There is no direct
   escalated-to-in_repair path.
6. Preserve deferral labels. Do not convert a deferral into a technical fix, release
   an escalated issue through an ordinary resolution, or request a new allowance
   without separate human approval.

### 6.2 Scoped acceptance and deferral

A scoped human acceptance or deferral applies to `pending`, `in_repair` or
`escalated` and finishes the issue. It requires the scope, the owner disposition and
the acceptance evidence. A deferral also names the approved stage it is deferred
for. Record that the work is deferred for that stage, not technically fixed.

A deferral alone is not historical continuation authority, and it does not permit
another automatic repair attempt, another reassignment or a counter reset.

### 6.3 Interrupted save and audit reconciliation

`Engine.record_human_disposition` saves a durable operation intent and the guarded
transition before appending the single `human_disposition` audit. If the process
stops between those writes, the run retains the transition but no audit. Resume
reports that operation as an audit gap; the transition must not be presented as
completed acceptance and must not be replayed.

For an already-saved authorized transition, use
`Engine.reconcile_human_disposition` with the same `HumanDisposition` that was
authorized. Before writing anything, reconciliation validates:

- the exact issue and the supplied kind, scope, evidence, allowance disposition,
  dependencies and deferred stage;
- that the latest retained `lifecycle_history` entry is exactly the transition the
  supplied disposition would have produced from its recorded prior status,
  including guard and evidence;
- that exactly one retained history entry matches that transition;
- that the current lifecycle equals the retained result;
- unchanged attempt, counter and assignment history (exact operation digests when
  the durable intent exists, or an independently supplied accounting digest);
- that any existing audit for the same transition agrees with the disposition.

It appends at most one audit carrying `disposition_kind`, the retained
`transition_recorded_at`, `reconciled: true` and the actual `reconciled_at` time.
It never reapplies the transition, resets status, changes counters or assignments,
rewrites `lifecycle_history`, or fabricates the original event time or ID.
Repeated reconciliation returns the existing audit and appends nothing. Missing or
conflicting attribution is refused, and an audit that does not identify the
retained transition is never reused as its evidence. A finished status alone is
never eligibility. If the original durable transition or its authorization cannot
be proven, stop for a human disposition instead of backfilling.

## 7. Uncertain process ownership

Uncertain process ownership blocks redispatch. The absence of a process-start
record is not proof that nothing started.

1. Stop the run and preserve the run directory and its worktrees together.
2. Do not launch a duplicate process, kill a process whose identity is not
   positively confirmed, clear an uncertainty marker, delete evidence, or edit
   state.
3. Inspect the existing recorded attempt and its activity: the dispatch intent, the
   uncertainty event, any process events for the attempt, worker process identity,
   logs and handover path.
4. Use the existing audited uncertain-attempt recovery procedure in
   `20261030_resume_and_preflight_recovery.md` to establish and record a safe
   disposition.
5. Resume redispatch only after the disposition is recorded. Preserve every
   counter, approval and prior handover.

The recovery helper is the last line of defence for legacy and damaged evidence. If
it refuses, stop and escalate; do not retry with different arguments.

## 8. Carry-forward inventory

A carry-forward run requires separate approval and a reviewed Markdown inventory. It
establishes a current acceptance path. It does not certify historical recovery and
does not replenish budgets.

The inventory must preserve predecessor linkage, original issue and assignment
identities, counters and exhausted allowances, dispositions, evidence references,
solved-work evidence and explicit missing historical proof. An incomplete inventory
is refused; never default exhausted issues to fresh allowances.

| Issue | Carry-forward disposition | Retained allowance and uncertainty |
|---|---|---|
| `code-w01-01` | Resolved for its recorded scope. | Do not reopen, re-fix or drop merely because a new run is created. |
| `code-w01-02` | Current-stage acceptance deferral. | Native Windows evidence remains unverified; the issue is not technically fixed. |
| `code-w01-03` | Historically escalated. | Original and replacement allowances are exhausted; no further automatic repair or reassignment. |
| `code-w01-04` | Resolved for its recorded scope. | Preserve history; do not silently reopen. |
| `code-w01-05` | Resolved for its recorded scope. | Preserve history; do not silently reopen. |
| `code-w01-06` | Resolved for its recorded scope. | Preserve history. The recorded disposition does not by itself supply the missing historical decision observed in the review evidence. |
| `docs-w01-01` | Resolved for its recorded scope. | Preserve history. |

`LIVE-REVIEW-003` and its derived findings, depth stops and retired assignments are
preserved. Any new implementation assignment must state its approved scope and
allowance disposition explicitly. A fresh assignment does not imply replenishment.

| Rule | Retained status |
|---|---|
| Repair attempts | Maximum three per assignment; stop after two unsuccessful repairs. |
| Reassignment | Maximum one per issue. |
| Review rounds | Existing limit, unchanged. |
| Issue lineage and depth | Existing independent limits, unchanged. |
| Malformed verdict | One repair call before escalation. |
| Historical counters and stop history | Preserved, never reset. |
| Fresh branch, worktree, run or publication sequence | Grants no new allowance. |

Explicit uncertainties to keep visible:

- Exact future candidate identities, dispatch selections and review outcomes are not
  yet available.
- Historical evidence gaps remain unproven. The original run remains stopped, and a
  new implementation cannot certify missing historical decisions.
- `code-w01-02` native Windows evidence remains unverified.
- A legacy ambiguous `open` status cannot be resolved from counters alone; an
  explicit mapping is required.
- The carry-forward run requires separately approved inputs and a reviewed
  inventory; the approved plan does not create or authorize that run by itself.
- Runtime and cost authority must be rechecked before future live operations; no
  extension was granted.
- Accepted numeric diagnostic-location caveats remain; disclosure remains blocking.
- The rejected integration `fa571dbcfab3cfd2e0c5edb15abf08cdb819c0a4` is evidence
  only, not the implementation architecture.

## 9. Disclosure reconciliation and current acceptance

The disclosure work concerns product candidate
`de066bc7663cab6d3cd5d896b0c8e0b8b7174bce` and disclosure fix
`1ef6041c2f0d01712d9b038aa5bc5dd57d214b12` with its product parent. The fix is not
already integrated into the installed runner. Its source-disclosure correction was
independently confirmed, while numeric diagnostic-location defects were accepted as
Fedora stage-one caveats.

Reconcile the reviewed product candidate and fix with the reviewed runner source
through reviewed source comparison. Do not blindly cherry-pick across differing
ancestry or replace workflow files.

Permitted authority covers incorporation of already-produced disclosure work,
independent code and documentation review, and a current acceptance disposition for
`code-w01-03` on the exact assembled candidate under the amended Fedora scope. It
does not permit another automatic repair attempt, another reassignment, a counter
reset, or treatment of a deferral as historical continuation authority.

If reconciliation needs substantive new repair, or acceptance fails, stop for human
disposition. Accepted numeric diagnostic-location caveats remain, and disclosure
remains blocking.

## 10. Stop conditions

Stop and preserve the run for human disposition on any of:

- conflicting provenance;
- ambiguous repair-outcome attribution;
- an illegal or unlisted lifecycle transition;
- uncertain process ownership;
- exhausted allowances;
- an applicable depth limit;
- a substantive new disclosure repair requirement;
- failed acceptance;
- required changes outside approved ownership or scope.

Also stop if the guarantees would require a replacement evidence repository, a broad
checkpoint framework or a general scheduler rewrite. Return for architecture
discussion rather than widening the assignment.

Do not reset counters, edit state or the journal, delete evidence, fabricate a
handover, or force progress to avoid a stop condition.

## 11. Verification and acceptance expectations

Fedora end-to-end acceptance must record:

- the exact reviewed candidate;
- the integrated or promoted feature identity where applicable;
- the current approved inputs;
- the independent verdicts;
- the verification results actually observed;
- the human Fedora outcome.

Isolated fault-injection tests establish crash and replay guarantees. A successful
live exercise does not prove missing historical decisions. Native Windows and
PowerShell remain deferred and unverified, including ACL, replacement, locking and
interruption behavior. Accepted diagnostic-location caveats do not waive disclosure
or accounting integrity.

Observed on assembled integration `8ad1901bbc49d343f3633220ac78723c0264907e` (W03
source `4e6df8ae6872d332a5809cb5d589450097a5934c`, W03 documentation
`eb41010511d503bcf55cb91b97336db9b770f959`): `uv run pytest -q` reports 370 passed
and 5 skipped, with `uv run ruff check .` and `git diff --check` clean. The five skips
are one pre-existing skip and four explicit absent-prerequisite skips for legacy
live-evidence recovery. Those four need a host that retains the exact
`round-N/verdict-code.md` and `round-N/verdict-docs.md` batch; the retained live run
advanced to round 5 with attempt-suffixed verdict names, so that batch is absent. No
assertion was weakened; the ownership-conflict and recovery-replay guarantees are
exercised synthetically.

W03 records its handover as partial and states the remaining limits. CB-D3 (completion
evidence reconciled before its audit) has no isolated synthetic test because it belongs
to the process-dispatch pipeline, which the tests do not start; CB-D1, CB-D2 and CB-D4
are covered by S07. CB-F0 and CB-F1 are not claimed because this baseline has no
carry-forward run construction; S08 binds preservation to the committed explicit
migration authority instead. CH01 and CH02 (approval hash and candidate association)
are covered by the existing Engine-level approval test in the routing module, not by a
new synthetic test.

This is Linux-local evidence on the assembled integration, not Fedora acceptance. No
live operation, live recovery, Windows or PowerShell verification is claimed.

The documentation reviewer receives this runbook, the normative application
document and code-free handovers; no code inputs.

## 12. Related documents

- `20261109_review_lifecycle_application.md` — normative lifecycle, transition,
  accounting, application, replay, evidence and compatibility contract.
- `20261026_contracts_and_configuration.md` — schemas, settings and durable-state
  evidence.
- `20261030_independent_review_routing.md` — reviewer ownership and per-entry
  application.
- `20261030_resume_and_preflight_recovery.md` — prelaunch rejection, uncertain
  attempts and audited recovery.
- `20261026_git_and_recovery.md` — worktrees, integration, promotion and cleanup.
- `20261026_user_guide.md` — command overview and platform boundaries.
- `20261026_verification.md` — automated checks and unverified platform gates.
