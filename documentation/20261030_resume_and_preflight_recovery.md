# Resume, preflight rejection and uncertain launch recovery

This note explains how the runner distinguishes a launch that never started from
one whose ownership is unknown, and how an operator resolves a recorded uncertain
attempt without hand-editing state or fabricating evidence.

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

- `activity_log.md` and `events.jsonl` contain the attempt's `dispatch_intent` and
  its `agent_launch_uncertain` record;
- no `process_started` or `process_children` event carries that attempt identity;
- no process identity or PID is recorded for it in `state.json`;
- the corresponding `round-<n>/logs/<attempt>/` directory does not exist, which
  is consistent with rejection before the adapter created logs.

If any process identity exists, stop. The helper refuses that case and the
attempt requires escalation, not recovery. Do not kill an unverified process and
do not delete or edit journal events.

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
requires the run to be `PAUSED` or `ESCALATED` and holds the active-run lock, so
it cannot run beside a live engine. It refuses an attempt that has no recorded
intent, no recorded uncertainty, terminal evidence or any process identity
evidence. It preserves the attempt counter, retry counters, approvals and
previous handovers, clears only the uncertainty marker and appends a
`uncertain_attempt_resolutions` history entry. The decision, operator and reason
are journaled as `uncertain_attempt_resolved`.

## Order of operator steps

1. Stop the runner and back up the run directory and its worktrees together.
2. Review the attempt evidence as described above.
3. Run the audited recovery helper. It must come before approval, because it
   requires a paused or escalated phase.
4. Run `orch approve <slug>`. Approval re-binds the current effective settings
   and records the current feature head, including orchestrator report commits
   written by earlier escalations.
5. Run `orch resume <slug>` and watch the next attempt. A missing optional
   handover is skipped explicitly; a new launch receives a new attempt identity.

A resumed launch that exits non-zero remains an ordinary infrastructure failure
and follows the existing retry budget. Do not reset counters or hand-edit
`state.json`, `events.jsonl` or the Markdown evidence files to force progress.
