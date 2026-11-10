# Review lifecycle, per-entry application and replay

- Date: 2026-11-09
- Status: documentation of the committed review-lifecycle contract
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
  gates are recorded in section 12. No Fedora acceptance or live operation is
  claimed.

Contract and reducer behavior below remains bound to the two committed interfaces;
the observed suite is verification evidence, not a substitute for them.

This note is the normative account of one issue's writable lifecycle, the separate
records that surround it, the guarded transitions an operator may rely on, and the
durable per-entry application that makes a review batch safe to replay. It is
written for a documentation reviewer and an operator; no code is required. The
procedure an operator follows is in
`20261109_review_lifecycle_operations.md`.

## 1. Why the lifecycle is separate from accounting, activity and application

The repaired contract answers four different questions with four different records.
Keeping them separate is the core correction: before it, a review batch could record
completion after the first entry escalated, and reconstruction then expected
decisions for every referenced entry.

| Dimension | Question it answers | Typical records | It is not |
|---|---|---|---|
| Lifecycle | What is the issue's writable status, and which transition is legal now? | One lifecycle status per issue; committed transition history. | Repair history, review completion or acceptance proof. |
| Counters and assignment history | What allowance has been spent, and which assignment and repair ordinal owns the outcome? | Repair attempts, unsuccessful repairs, assessed repair high-water mark, charged ordinals, review rounds, reassignments, assignment identity and history. | A lifecycle status, current activity, or proof that an assessment happened. |
| Activity | What is happening now? | Dispatch, check, review, repair, process and lifecycle activity records; process ownership is known or uncertain. | Budget spend or durable application. |
| Application records | Which review entries have a committed disposition, and where does replay resume? | Review application intent, entry identities and order, per-entry decisions, audit events, next-entry position, completion condition. | Issue completion, a passing review or promotion eligibility. |

Acceptance and deferral evidence is a separate human decision record. A deferral is
recorded as deferred for the approved stage; it is not described as technically
fixed.

## 2. Vocabulary

### 2.1 Lifecycle status

There is exactly one writable lifecycle status field per issue. Display detail is
derived from records; there is no independently writable duplicate substatus.

| Status | Meaning | Must not be read as |
|---|---|---|
| `pending` | The issue is not dispatched. It may be new work, work returned by a valid reopening, or work returned by explicit human continuation. Dispatch still needs the normal guard. | "Ready to dispatch unconditionally", "approved", or "never worked". |
| `in_repair` | An authorized assignment is being worked, or a permitted subsequent attempt or reassignment is in progress. Previous assignments are retained. | "One attempt only", or "reassignment clears history". |
| `finished` | Owning-reviewer acceptance for the relevant candidate, inputs and outcome, or an explicit scoped human acceptance/deferral with the required owner disposition and evidence. | "The review passed", "the run is done", or "promotion is eligible". |
| `escalated` | Progress stopped by explicit escalation, allowance exhaustion, a depth rule, an applicable stop, or attribution that cannot safely authorize continuation. Human disposition is required. | "Automatically recoverable", "safe to retry", or "released by an ordinary resolution". |

Only these four values are written by new code. Version-one records keep their
historical `open` and `resolved` values; conversion is covered in section 10.

### 2.2 Evidence kinds and evidence classes

An evidence *kind* says what a record is. An evidence *class* says how far it may be
trusted for a particular consumer. They are separate facts.

Evidence kinds relevant to the lifecycle, in the stage where each applies: original
input selection and approval; candidate binding; reviewer ownership; selected
attempt; dispatch and completion evidence; verdict identity; accounting intent;
applied entry decisions; acceptance and audit events.

| Class | Meaning | Permitted use | Operator handling |
|---|---|---|---|
| Verified | Every prerequisite appropriate to that consumer holds. | Authoritative use for that consumer only. | May support the guarded transition or accounting decision. |
| Recomputed unbound | Internally consistent retained application without the original candidate/result binding. | Diagnostic comparison only; never invent a historical binding. | Explain what is unbound; do not present it as historical proof. |
| Unverifiable — missing inputs | Required prerequisites are absent. | Explain the missing prerequisites; refuse authoritative consumption. | Stop if the missing input is required for the action. |
| Conflict | Retained evidence contradicts itself or another required record. | Stop the affected application; stop run-wide work where accounting or review trust is compromised. | Preserve the run and escalate; do not choose a side by counter value. |

A missing digest and a missing input are different facts and are reported
separately. Human authority may authorize a new current assessment; it cannot turn
missing original proof into verified historical evidence.

### 2.3 Wording rules

- "Application complete" means every review entry has a validated disposition. It
  does not mean the issue is finished, the review passed, or the candidate may be
  promoted.
- "Finished issue" does not mean a finished run.
- "Retained", "exhausted" and "unchanged" describe counters. "Reset",
  "replenished" and "fresh allowance" are not used for an existing issue merely
  because a branch, worktree, run or publication sequence is new.
- "Deferred for the approved stage" is not "fixed".
- "Uncertain" process ownership is not "safe to relaunch".
- Historical evidence is not upgraded by a current human decision.

## 3. Lifecycle statuses, history and legacy values

One status field carries the lifecycle. Every committed transition appends one
history record naming the issue, the source and target statuses, the satisfied
guard and the evidence. Status changes never alter counters, assignments, review
rounds or outcomes.

A version-one `open` record with no repair, failure or review activity is displayed
as `pending`. A `resolved` record is displayed as `finished`. An `escalated`
record is displayed as `escalated`. A version-one `open` record that has any
repair, failure or review activity is ambiguous: it is displayed as `ambiguous`
and refused for operational use until an explicit mapping names `pending` or
`in_repair`. It is never guessed from counters. The original historical value is
retained and rendered as a legacy value so the record is not falsified.

## 4. Guarded transitions

Transition identifiers below (`L1`–`L10`) are documentation labels for
traceability only; they are not persisted values. Guards are conjunctions of
explicit facts. Every transition not listed is rejected before mutation, and a
listed transition with an unmet guard is rejected before mutation.

### 4.1 Normal progress

| ID | From → to | Required guard |
|---|---|---|
| L1 | New issue → `pending` | Stable identity, owner and lineage; creation never implies dispatch. |
| L2 | `pending` → `in_repair` | Valid identity and owner lineage, durable dispatch intent, ready dependencies, available allowance and known process ownership. |
| L3 | `in_repair` → `in_repair` | Authorized subsequent attempt **or** permitted reassignment; previous assignments are retained. |
| L4 | `in_repair` → `finished` | Owning-reviewer acceptance **or** scoped human acceptance with owner disposition and evidence. |

### 4.2 Reopening, attribution and stopping

| ID | From → to | Required guard |
|---|---|---|
| L5 | `finished` → `pending` | Valid reopening, exact historical attribution and remaining authorized work. |
| L6 | `finished` → `escalated` | Valid reopening plus an applicable stop boundary **or** valid reopening plus attribution that cannot safely authorize continuation. |
| L7 | `pending` → `escalated` | Explicit escalation **or** allowance exhaustion **or** depth stop **or** another applicable stop rule. |
| L7 | `in_repair` → `escalated` | The same four alternatives as pending. |

### 4.3 Human authority

| ID | From → to | Required guard |
|---|---|---|
| L8 | `pending` → `finished` | Explicit scoped human acceptance plus owner disposition and evidence. |
| L8 | `escalated` → `finished` | The same scoped human acceptance guard. |
| L9 | `escalated` → `pending` | Explicit human continuation covering scope, allowance disposition and dependencies; counters are unchanged. |

`in_repair → finished` also accepts scoped human acceptance, as noted under L4. The
contracted alternatives are the authoritative list; a caller cannot combine
partial facts into an unlisted transition.

### 4.4 No-change entry

| ID | From → to | Required guard |
|---|---|---|
| L10 | Any status → the same status | A listed same-status justification, otherwise validated application evidence and a no-change decision. |

A no-change entry is a real applied entry. It records an explicit decision and
invents no charge.

### 4.5 Workflow-owned check issues

Issues created by the workflow under `check-*`, `post-check-*` and
`integration-check-*` identities are owned by the `system` role. They are created
in `pending` with deterministic check evidence, and they are finished by check
evidence through the system-only rule for `pending` or `in_repair` to `finished`.
That rule is not a code or documentation reviewer bypass: a reviewer cannot report
or resolve a system-owned issue through the ordinary reviewer path, and a
differing ownership role is a conflict for the evidence authority.

### 4.6 Refusal and preservation

| Situation | Documented behavior |
|---|---|
| Unlisted transition | Refused before mutation. |
| Listed transition with an unmet guard | Refused before mutation; the issue keeps its prior status and counters. |
| Ambiguous repair-outcome attribution | Application refused before mutation. |
| Stale review trying to close newer work | Refused; a stale review cannot close newer work. |
| Repeated ordinary resolution of an escalated issue | Refused; it cannot silently release the issue. |
| Conflicting evidence | The affected application stops; run-wide work stops where accounting or review trust is compromised. |
| Incomplete carry-forward inventory | Refused; exhausted issues are never defaulted to fresh allowances. |
| Uncertain process ownership | Redispatch is blocked until the existing audited recovery procedure establishes a safe disposition. |

### 4.7 Continuation invariant

Human continuation returns an issue to `pending`, never directly to `in_repair`.
Any later dispatch then passes the normal `pending → in_repair` guard. There is no
automatic escalated-to-in_repair shortcut. Continuation changes no counter, and a
new branch, worktree, run or publication sequence grants no new allowance.

## 5. Counters, assignments and accounting

The retained limits are unchanged:

- up to three repair attempts per assignment;
- stop after two unsuccessful repairs per assignment;
- maximum one reassignment per issue;
- the existing review-round limit;
- the existing independent issue lineage and depth limits;
- one repair call before escalation for a malformed verdict.

Repair outcomes are recorded per issue, assignment identity and repair ordinal.
Ordinal-zero initial work is recorded as initial work and never charged as a failed
repair or a review round. Each actual unsuccessful repair is charged once. A new
failure charges the failed-repair and review-round counters once.

Duplicate review, repeated findings and approval-only revalidation of the same
outcome return an already-charged or already-assessed result and create no new
worker outcome or allowance charge. An approval-only change can force a fresh
reviewer run and a new role result against the current inputs without creating a
worker repair.

A valid reopening of finished work supersedes the earlier successful assessment
while preserving it. The repair that produced the defective fix is charged once as
a superseding failure, on the exact assignment and repair ordinal that produced it,
even when that assignment is older. A newer unrelated assignment is not charged.
The reopening itself creates no repair attempt and consumes no additional review
round. Repeated reopening of the same outcome returns already-charged.

If two assignments share a repair ordinal and no assignment index is supplied,
attribution is ambiguous and application is refused before mutation. Counters
alone never prove an assessment: a completed review outcome with retained evidence
is required. The provenance digest of the reviewer evidence is retained on every
accounting decision.

## 6. Per-review application contract

Each accepted review is applied as one durable application. The application retains
at least:

- reviewer role and selected attempt identity;
- candidate identity;
- approved-input identity and bindings;
- verdict identity, digest and path;
- immutable pre-application issue evidence;
- deterministic entry identities and order;
- per-entry application state and decision;
- applied lifecycle and accounting decisions with evidence references;
- the derived next-entry position and the completion condition.

Every finding and every resolution produces exactly one entry. Entry order is
verdict finding order followed by verdict resolution order, with contiguous order
positions and unique identities. The contract forms identities from the entry kind
and issue identity, and the application identity is derived from the review
transaction identity. A resolution entry never becomes a repair request.

| Disposition | Meaning | Completion effect | Mutation effect |
|---|---|---|---|
| `not_applied` | No validated disposition has been committed for that entry. | Prevents completion. | None; the issue keeps its exact prior status and counters. |
| `applied` | A validated disposition, including a no-change decision, has been committed with its evidence. | Counts toward completion. | Only the recorded decision; a no-change decision makes no charge. |

The next-entry position is the first entry that is not applied, or the entry count
when all are applied. Applied entry identities and full application are derived
rather than stored separately. A no-change decision is still an applied entry with
an explicit decision.

Completion means every entry has a validated disposition. It does not imply issue
completion, a passing review or promotion eligibility. If entry A escalates,
untouched entries B–F retain their exact prior statuses and counters and remain
not applied until each is individually validated and committed. The caller supplies
the proposed decisions; the authority does not silently invent them.

The review transaction intent also retains the complete pre-application register,
the entry identities and the issue-scoped escalations. These payloads are additive;
existing payload fields remain.

## 7. Crash and replay protocol

The application follows one ordered protocol:

1. Validate review identity, ownership, original bindings and complete sheet
   structure.
2. Persist an intent containing all entry identities and the deterministic proposed
   decisions.
3. Atomically save an entry's issue and counter effects together with its applied
   decision.
4. Record its journal event using the stable operation and entry identity.
5. Reconcile a missing audit event from the durable decision; refuse contradictions.
6. Continue remaining entries after an issue-scoped escalation.
7. Resume unfinished application before relevant dispatch.
8. Mark application complete only when all entries and required audit evidence agree.

| Crash boundary | Expected recovery |
|---|---|
| Before intent persistence | No application effects exist; revalidation may start cleanly. |
| After intent, before any entry save | The intent records the proposed decisions; there are no issue or counter effects; resume from the recorded plan. |
| Between an entry save and its audit event | The durable decision exists; reconciliation recreates the missing audit event; nothing is charged twice. |
| Between entries | Applied entries stay applied; the remaining entries continue from the recorded plan. |
| After all entries, before completion | Completion is reconciled only when entries and audit evidence agree. |
| After completion | Re-entry returns the recorded result; there is no second application and no new decision. |

Reconciliation refuses an audit event for an entry that is not applied, a duplicate
audit event, and a digest mismatch. Resume reports whether the application is
absent, pending or completed, the next-entry position, the applied entry identities
and the completion flag. It launches no worker and does not dispatch.

Replay uses retained evidence and committed decisions, never current counters as
substitute proof. Application launches no worker; dispatch retains its own durable
intent and result protocol. Uncertain process ownership blocks redispatch until the
existing audited recovery procedure establishes a safe disposition.

## 8. One authority and evidence checks

Review acceptance, accounting reconstruction, interrupted-operation replay,
transaction gap checks and the existing recovery paths consume one deterministic
accounting and routing authority. No optional caller mode may bypass mandatory
reviewer provenance: a missing provenance, missing ownership, or missing dispatch or
completion evidence is unverifiable and refused. A pure planning projection exists
for a legacy register, but it is not an authoritative acceptance path.

The authority checks the prerequisites appropriate to the operation's stage, using
the evidence kinds in section 2.2. The supported consumer operations and the
prerequisites they require are:

| Operation | Required evidence |
|---|---|
| Acceptance | Accepted result, candidate binding, reviewer ownership, verdict identity, accounting intent, applied entry decisions, acceptance events and completion. |
| Reconstruction | Accepted result, candidate binding, verdict identity, applied entry decisions, acceptance events and completion. |
| Replay | Candidate binding, verdict identity, accounting intent and applied entry decisions. |
| Gap check | Candidate binding, verdict identity, accounting intent and applied entry decisions. |
| Recovery | The same prerequisites as acceptance. |

The four evidence classes and their permitted uses are exactly those in section
2.2. Verification requires the prerequisites for that consumer; a conflict raises a
conflict failure and stops the affected application; an unverifiable assessment and
a recomputed-unbound assessment refuse authoritative consumption. Only the
authoritative entry point may be consumed as proof.

A missing digest and a missing input remain distinct facts. Human authority may
permit a new current assessment; it cannot create verified historical evidence from
missing original proof.

Gap checking reports a missing application, the named unapplied entries, a missing
completion record, missing or duplicated entry audits, and a non-verified evidence
classification. Empty-entry transactions remain valid without an application
record.

## 9. Dependency handling

Dependency handling is the documented minimum, not a scheduler rewrite.

The persisted dependency record is one physical Markdown triage record declared by
the manifest, associated with the source verdict identities and the approved plan
version. It records stable issue and owner identifiers, semantic prerequisites and
their rationale, explicit independent findings, shared-resource constraints and
verification dependencies. Reviewers propose dependencies; a separately authorized
qualified assessor is used when ambiguity or cross-role technical complexity
justifies it, and documentation-only reviewers receive code-free material.

The coordinator validates known issue identifiers, known worker ownership, approved
shared-path scopes and graph acyclicity, and requires the proposals to be
consistent. A missing or invalid declared record refuses scheduling rather than
choosing an interpretation. Unknown, cyclic or contradictory dependencies block the
affected dispatch for reassessment.

Scheduling then applies these rules:

- shared resource paths serialize work; at most one issue is selected per declared
  shared-resource key, including keys already active in another worker's wave;
- semantic dependents wait until their required prerequisite issues are finished;
- verification dependents wait for assembled review-acceptance evidence or a
  recorded artifact key, not a worker's claim in a handover;
- waiting issues are not dispatched and consume no attempt;
- independent ready repair requests may proceed after the required review entries
  have been applied;
- resolution entries never become repair requests;
- existing concurrency and immutable-snapshot gates remain;
- unresolved blocking issues still prevent promotion.

When a configured dependency record is present it participates in approved-file
hashing exactly like the other reviewed plan inputs. Its recorded digest binds the
record to its reviewed content. If verification readiness needs a different
assembled-evidence source, the manifest record must name that source explicitly.

Promotion is gated on both accepted role results passing for the same immutable
candidate, a complete application with every entry applied and audited, no
unfinished or escalated issue without an explicit disposition, no unresolved
blocking issue, and complete journaled source coverage for the exact reviewed head.
A promotion attempt is refused with the named blockers rather than proceeding
around them.

## 10. Schema, compatibility and carry-forward

Newly created runs use an explicit state schema version two. Loading accepts schema
versions one and two only; a missing version or any other value is refused with an
error naming explicit migration. Loading version-one bytes does not rewrite them.

Version-one rendering keeps the historical `open` or `resolved` status and adds the
derived lifecycle value; a version-one `open` record with repair activity renders
`ambiguous` rather than being guessed. Operational use of version-one records
requires an explicit compatible migration:

- the migration produces an explicit version-two copy and preserves counters,
  evidence, dispositions, legacy ownership and lineage;
- it refuses an `open` issue with repair activity unless an explicit `pending` or
  `in_repair` mapping is supplied, and refuses unknown or incomplete mappings;
- it refuses an incomplete assignment-identity inventory when assignment identities
  are being established for attribution;
- the original historical status is retained as a legacy value.

Operational use of version-one state, or of any remaining legacy status in
version-two state, is refused rather than silently reinterpreted. Broad in-place
historical migration is outside this delivery; the original run remains stopped.

Carry-forward initialization preserves original issue and assignment identities,
all counters and exhausted allowances, dispositions, evidence references and
explicit historical uncertainties. An incomplete inventory is refused instead of
defaulting exhausted issues to fresh allowances. A fresh assignment never implies
replenishment.

A verdict-schema extension, if any, must be explicitly versioned and compatible.
The separate Markdown dependency record is preferred over making additional verdict
fields mandatory.

## 11. Boundaries and non-goals

This lifecycle work includes the four statuses and their guarded transitions,
separate counters and activity, durable per-entry application, crash-safe replay,
once-per-repair accounting, one deterministic authority with mandatory evidence,
independent eligible work after issue-scoped escalation, minimum dependency
representation, compatible schema handling and reviewed carry-forward preparation.

It deliberately does not include an archive or checkpoint redesign, a replacement
evidence repository, a general scheduler rewrite, unrelated dashboard or diagnostics
work, or broad in-place historical migration. The rejected integration
`fa571dbcfab3cfd2e0c5edb15abf08cdb819c0a4` is evidence only and is not the
implementation architecture.

## 12. Verification status

The document above is bound to the committed contract and reducer interfaces at the
source identifiers at the top of this note. The observed evidence below is the
Linux-local suite on the assembled integration; it is not Fedora acceptance.

- Integration `8ad1901bbc49d343f3633220ac78723c0264907e` (W03 source
  `4e6df8ae6872d332a5809cb5d589450097a5934c`, W03 documentation
  `eb41010511d503bcf55cb91b97336db9b770f959`) records `uv run pytest -q` as
  370 passed and 5 skipped, with `uv run ruff check .` and `git diff --check`
  clean.
- W03's whole-chain module implements the approved matrix scenarios S01–S10 within
  the baseline scope stated below. The confirmed observations include A–F escalation
  with untouched entries preserved, per-entry crash injection at every position for
  one-, two- and six-entry batches, golden-projection replay equivalence, duplicate
  and approval-only review, reopening and already-charged outcomes, ordinal-zero work,
  historical attribution, every legal and illegal transition, dependency waiting and
  shared-path serialization, blocking-issue promotion refusal, uncertain dispatch, and
  the distinct evidence classes.
- The five skips are one pre-existing skip
  (`test_retained_live_round_two_revalidation_...`) and four explicit
  absent-prerequisite skips. The four legacy live-evidence recovery cases skip because
  the retained live run advanced to round 5 with attempt-suffixed verdict names, so the
  exact `round-N/verdict-code.md` and `round-N/verdict-docs.md` batch consumed by the
  legacy recovery helper is no longer present. Re-running them requires a host that
  retains that exact batch. No assertion was weakened; the ownership-conflict and
  recovery-replay guarantees are exercised synthetically by S07 and S09.
- W03 records its handover as partial for the remaining limits. CB-D3 (completion
  evidence reconciled before its audit) has no isolated synthetic test because it
  belongs to the process-dispatch pipeline, which these tests deliberately do not
  start; CB-D1, CB-D2 and CB-D4 are covered by S07. CB-F0 and CB-F1 are not claimed:
  this baseline has no carry-forward run construction, and S08 binds preservation to
  the committed explicit-migration authority instead. CH01 and CH02 (approval hash and
  candidate association) are covered by the existing Engine-level approval test in the
  routing module, not by a new synthetic test.
- No Fedora end-to-end acceptance, live continuation, live recovery, additional
  automatic product repair or reassignment is claimed. Native Windows and PowerShell
  remain deferred and unverified.

## 13. Related documentation

- `20261109_review_lifecycle_operations.md` — operator runbook for reading a run,
  applying transitions, unfinished batches, replay and continuation.
- `20261026_contracts_and_configuration.md` — schemas, settings, durable state and
  evidence locations.
- `20261030_independent_review_routing.md` — reviewer roles, ownership and per-entry
  review application.
- `20261030_resume_and_preflight_recovery.md` — prelaunch rejection, uncertain
  attempts and audited recovery.
- `20261026_git_and_recovery.md` — worktrees, integration, promotion and cleanup.
- `20261026_user_guide.md` — command overview and platform boundaries.
- `20261026_verification.md` — automated checks and unverified platform gates.
