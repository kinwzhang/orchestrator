# Print current status when orch next returns

## User requirement

When orch next returns, print the run's current status so the operator does not need a separate orch status command to understand the resulting state.

## Behavior and acceptance

- After a successful step, show the persisted phase, stage, cycle and next action using the same concise status presentation as orch status, including relevant worker/reviewer statuses, open issues and blocker information.
- When next returns at a human gate or after a handled failure/escalation, retain its explanatory message and print the current persisted status as well. Preserve the existing exit code.
- Report persisted state after stage handling and reconciliation, rather than a stale in-memory snapshot. Do not advance another stage, launch another agent, acquire extra workflow authority or mutate the run merely to print status.
- If status cannot be loaded safely, retain the original failure and exit code and report that status is unavailable. Do not fabricate success or let a secondary formatting failure hide the original error.
- Keep output concise and avoid duplicating the same summary within a single invocation. Invalid arguments or failure to identify a run need not print a run summary.
- Verify success, human-gate and failure/escalation cases with stub agents and temporary run data. Confirm unchanged lifecycle behavior and exit codes.

## Delivery boundary

Implement in a future approved CLI assignment. This document does not alter the active model-preferences-cli run, its inputs, approvals, state or installed runner. No paid or live agent calls are needed for verification.
