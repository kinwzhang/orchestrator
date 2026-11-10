# Resume, preflight rejection and uncertain launch recovery

This note explains how the runner distinguishes a launch that never started from
one whose ownership is unknown, and how an operator resolves a recorded uncertain
attempt without hand-editing state or fabricating evidence. It also covers the
review-application reconciliation that must finish before relevant dispatch. The
normative lifecycle, transition and application contract is
`20261109_review_lifecycle_application.md`; the operator runbook is
`20261109_review_lifecycle_operations.md`.

## Unfinished review application before dispatch

A review transaction can be durably complete while its per-entry application is not.
Before repair dispatch, at the start of each worker pass and during review
finalisation, the runner reconciles every durable but incomplete review application
from its recorded intent. It applies only decisions already persisted, recreates a
missing audit event from the durable decision, and completes the application only
when every entry and every audit agrees. It launches no worker and does not dispatch.

An incomplete application is a gap, not a completed review. A missing application
record, named unapplied entries, a missing completion record, missing or duplicated
entry audits and a non-verified evidence classification are all reported. Promotion
is refused while an application gap remains. Replay reads the recorded intent, base
register, verdict and per-entry decisions; current counters are never substitute
proof. A contradiction stops the affected application instead of choosing an
interpretation.

If reconciliation refuses, stop and preserve the run. Do not fabricate an entry
decision, delete an audit event or hand-edit state. An unfinished application and
uncertain process ownership are separate blocks: one can be resolved while the other
still prevents redispatch.

## Prelaunch rejection versus uncertain launch

Before any dispatch, the workflow validates every mandatory instruction and input
path, the Markdown result path and the log directory. The adapter repeats the
same validation immediately before it probes the CLI and creates a process. A
mandatory file that is missing, is not Markdown or has been replaced is reported
with its exact kind and path, for example `instruction`, `input`, `output` or
`log_dir`.

A failure in that validation is a *proven prelaunch rejection*. It is recorded as
`dispatch_preflight_failed` and the worker is marked `preflight_failed`. It never
creates a launch intent that a later resume might mistake for a possible launch,
and it never sets `uncertain_attempt_id`. Fix the missing or invalid file,
approve again if an approved input changed, and resume. The next attempt receives
a new attempt identity.

A failure that could have happened after process creation remains fail-closed. If
the workflow sees a launch intent but no recorded process identity, it records
`agent_launch_uncertain`, sets `uncertain_attempt_id` and refuses to relaunch.
Absence of a process-start event is not treated as proof that nothing started.
Resume keeps refusing until an operator establishes what happened.

The same uncertain marker is set when a dispatch is cancelled or paused before a
process identity is recorded. That case is journaled as
`agent_interruption_uncertain`. The first repair candidate's helper looked for a
different spelling, `agent_interrupted_uncertain`; that spelling is now accepted
only as an explicitly supported legacy form for evidence already written by that
candidate. The workflow never emits it. Do not rename or otherwise relabel an
existing event.

## Optional previous-handover evidence

`workers.<id>.handover` records where an attempt was asked to write. It is not
proof that a handover exists. Before a retry, the workflow includes that path only
when all of the following hold:

- the path is a regular `.md` file inside this run directory;
- it parses as a valid handover contract;
- its `agent` matches the worker being dispatched.

Otherwise the path is omitted and `handover_evidence_skipped` records the reason.
Retained `partial` handovers are therefore supplied to the next attempt, while a
missing or stale identity is never passed to an agent and never replaced with a
fabricated file.

## Audited recovery of an uncertain attempt

An uncertain attempt cannot be resolved automatically, and the runner never
decides that it was safe from the text of an exception. Such attempts already
created under an older runner remain `uncertain` until a person reviews the
evidence and attests to the result. The audited recovery helper records that
decision permanently.

Before using it, confirm from the run directory that the attempt truly has no
process evidence:

- `activity_log.md` and `events.jsonl` contain exactly one `dispatch_intent` for
  the attempt and a recognized uncertainty event: `agent_launch_uncertain` for a
  launch that may have started, or `agent_interruption_uncertain` for a
  cancellation before process identity;
- no `process_started`, `process_children` or `process_completed` event carries
  that attempt identity;
- `state.json` records no `process` dictionary and no `process_attempt_id` for the
  worker, including a stale identity that belongs to another attempt;
- the corresponding `round-<n>/logs/<attempt>/` directory does not exist, which
  is consistent with rejection or cancellation before the adapter created logs;
- the recorded handover path does not exist because the attempt never wrote one.

### Stop, preserve and do not bypass

If any item above does not match, if the evidence is missing, duplicated,
contradictory, malformed or has changed since an operator decision was recorded,
or if the helper refuses for any reason, stop. Do not retry with different
arguments, clear a marker, delete or edit an event, remove a process field, kill a
process, rewrite `state.json` or fabricate a handover. Preserve the run directory
and its worktrees together and escalate for review. The helper is the last line
of defence for legacy and damaged evidence; a refusal is a reason to inspect, not
an obstacle to work around.

Run the helper from the reviewed runner checkout, with the run stopped and no
`orch resume` process active:

```bash
uv run python - <<'PY'
from pathlib import Path
from orch.store import StateStore
from orch.workflow import resolve_uncertain_dispatch

store = StateStore(Path.home() / ".local/state/orch")
event = resolve_uncertain_dispatch(
    store,
    "<run-id>",
    "<attempt-id>",
    operator="<your-name>",
    reason="<what you independently verified>",
)
print(event["kind"], event["id"])
PY
```

Use the configured data directory when it is not the Linux default. The helper
requires a paused or escalated phase for a new decision and holds the active-run
lock, so it cannot run beside a live engine. It refuses an attempt that does not
have exactly one launch intent and a recognized uncertainty event, or that has
terminal evidence, process identity in either the journal or worker state,
contradictory state, multiple resolution events or a conflicting recorded
decision. It never deletes process identity or other evidence.

The human decision, operator and evidence identities are persisted as an
operation intent before uncertainty can be cleared. The audit event is written
before the worker state becomes resumable, and the operation is then completed.
If an invocation is interrupted at any of those boundaries, resume is blocked
and re-running the helper finishes the same recorded operation using the
original operator, reason and evidence. Changed arguments on re-entry are
ignored; a second human decision is not requested and a duplicate decision is
never appended. A fully completed recovery returns its existing audit event and
performs no further side effects. It preserves the attempt counter, retry
counters, approvals and previous handovers, and appends one
`uncertain_attempt_resolutions` history entry.

## Retry budget inspection

The infrastructure retry limit is per manifest, under `limits` in the plan's
`manifest.md` (`max_infrastructure_retries`). The current counter for a worker is
recorded as `infrastructure_retries` in `state.json` and shown as `retries=` in
the run's `checklist.md` whenever it is non-zero. Read both before predicting what
a resumed launch will do; do not rely on another run's values.

The attempt counter is different from the retry counter. `attempt` counts all
cumulative dispatch attempts across rounds; `infrastructure_retries` counts the
automatic retries used for non-zero agent exits within the current dispatch cycle.
A success resets `infrastructure_retries` to zero. When the counter has reached the
manifest maximum, the next non-zero agent call escalates immediately instead of
retrying. Do not reset either counter to obtain another retry; preserve the
recorded budget and escalate.

As a dated observation only, the paused run `model-preferences-cli-5ec7f5ee` at
attempt `w01-1-4` records `infrastructure_retries` 2 and its manifest limit is 2,
so a non-zero resumed call would escalate immediately. Recheck that run's current
manifest and state before acting; this is not an assumption for other runs.

## Decidable worked example

The paused run `model-preferences-cli-5ec7f5ee` is the dated example for this
procedure. Before using it, recheck every value below; stop if anything differs.
The run-specific record with machine paths and input hashes is
`analysis_and_reviews/20261030_live_resume_case_w01_1_4.md`.

| Item | Expected value |
|---|---|
| Run | `model-preferences-cli-5ec7f5ee` |
| Worker / attempt | `w01` / `w01-1-4` |
| Attempt counter / retries | 4 / 2 |
| Manifest retry maximum | 2 |
| Launch intent event | `dispatch_intent`, event 32 |
| Uncertainty event | `agent_launch_uncertain`, event 33 |
| Process events for the attempt | none |
| State process identity | none (`process` and `process_attempt_id` absent) |
| `round-1/logs/w01-1-4/` | absent |
| `round-1/handover/w01-4.md` | absent |

Expected post-recovery state: phase remains `ESCALATED` until the next step;
worker status becomes `interrupted`; `uncertain_attempt_id` is absent; one
`uncertain_attempt_resolutions` entry exists; the operation
`uncertain-recovery:w01-1-4` is `completed`; and the journal contains one
`uncertain_attempt_resolved` event with evidence event IDs 32 and 33 and zero
process-identity events. Attempt 4, retries 2 and the approval hashes must be
unchanged. Re-entry returns the same event without adding another one.

## Order of operator steps

1. Stop the runner and back up the run directory and its worktrees together.
2. Confirm from the status or report views that no unfinished review application
   and no uncertain process ownership blocks the intended action. Reconcile the
   application or stop as described above; the two blocks are independent.
3. Recheck the evidence table above (or the generic checklist for another run).
4. Run the audited recovery helper. It must come before approval, because a new
   decision requires a paused or escalated phase. If it refuses, stop and
   preserve the run.
5. Run `orch approve <slug>`. Approval is required because the run's recorded
   feature head is stale: the stored head predates the escalated report commit
   written after the last approval. Approval records the actual head so the
   later reviewed-candidate head check can pass. Recheck approval separately; in
   this observed case the approval hashes already match and are not the reason
   for this step.
6. Run `orch resume <slug>` and watch the next attempt. A missing optional
   handover is skipped explicitly; a new launch receives a new attempt identity.
   For the worked example the next attempt is expected to be `w01-1-5`.

A resumed launch that exits non-zero remains an ordinary infrastructure failure
and follows the recorded retry budget. Do not reset counters or hand-edit
`state.json`, `events.jsonl` or the Markdown evidence files to force progress.
