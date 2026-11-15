# Operator visibility: status tables, next summaries and local activity times

This guide explains what the operator sees when inspecting a run with the status command, when asking the orchestrator to take the next step, and when watching a running dashboard. It describes meaning and behavior only. It does not require reading source files.

## Status at a glance

The status command answers four questions: which feature and run is being shown, where the delivery has reached, what should happen next, and whether a person must act.

The run summary table always shows the feature slug, the current lifecycle phase, the delivery stage, the delivery cycle number and the next action. It also shows the plan folder path and the run folder path. The plan path is where the approved planning documents live. The run path is where the orchestrator keeps state, evidence, handovers, verdicts and the Markdown checklist, activity log and issue register.

When the stored run records a blocker, a blocker line appears above the summary table so it is the first thing read. A blocker is a condition the orchestrator cannot resolve on its own, such as a failed check, an uncertain process, an approval change or an exhausted allowance. When the next action is a human gate, the summary also shows a human action row. That row states the kind of decision required: review and approve a plan, test the feature and accept it, resume a paused or escalated run, or recognize that the run is complete or aborted.

Status never changes the run. It does not advance a stage, dispatch an agent, write state or alter approval. Rendering reads the already-loaded snapshot and returns text.

## The agent table

The agent table lists every recorded role in a stable name order. Each row shows the agent or role name, its role, its status, its attempt number, its checks and its last recorded activity.

The attempt number identifies which dispatch the displayed information belongs to. Checks are deliberately conservative. A check result is shown only when completion for the current attempt was recorded. If an older check value remains in state while a newer attempt has not yet been confirmed, the table says the checks are not confirmed for this attempt instead of implying that the old result still applies. This prevents a successful earlier attempt from looking like a successful current attempt.

Raw agent logs are not placed in the status table. Log tails and live output remain the responsibility of the watch dashboard and detailed run evidence.

## The issue table

The issue table lists every issue in a stable identity order. Each row shows the issue identity, its current lifecycle, its attempt number, its reassignment count, its owning worker and a concise summary.

Lifecycle values are pending, in repair, finished and escalated. An issue that retains a historical status also shows a legacy annotation so the original record is not hidden. The annotation does not replace the current lifecycle. Historical counter values, including repairs, failed repairs and review rounds, are shown below the table with the rest of the issue detail. They are accounting facts, not lifecycle states.

Below the table, each issue records its root issue, its parent when it has one, its depth, its owner role if recorded, what it waits on, and the counters for each assignment. When a summary is too long for the table, an abbreviated form appears in the row and the complete summary is printed below the tables, so a long explanation never erases issue identity or blocker meaning.

## Review application progress

A review application table appears when a reviewer result is being applied to issues. It shows the application identity, the reviewer role, how many entries have been applied, the next entry position and whether the application is complete.

Application progress is separate from issue lifecycle. An issue may be finished while a review entry that also concerns it is still waiting to be applied, or the reverse. Pending application entries are listed below the table with their positions. The status view does not relabel issue status to describe review bookkeeping.

## Dependency records

When the approved plan includes a dependency record, a dependency table shows each dependent issue, the prerequisites it waits on, any shared resource paths and any verification prerequisites. This reflects recorded dependencies only. It is not a scheduling decision.

## Empty and narrow views

Clear messages replace empty sections. When no agents or issues are recorded, the output says so instead of showing an empty frame.

The layout adapts to the terminal width selected by the command. In a narrow terminal, agents and issues are printed as small labeled blocks rather than squeezed into columns. In a wide terminal, they appear as tables. Long paths and summaries wrap. Comparison between consecutive runs remains reliable because all sections use stable ordering. Plain redirected output contains no terminal color or escape sequences.

## Status after the next command

After the next command finishes a stage, stops at a human gate, or handles a failure or escalation, it prints exactly one current status. The status is the same detailed presentation used by the status command with a feature name. This means the operator does not need a second command to understand the resulting state.

The summary is produced from the persisted run after handling and reconciliation, not from an earlier in-memory copy. Printing it does not advance another stage, dispatch another agent or change lifecycle behavior. Explanatory messages, such as a human gate notice or the primary error, are retained. Exit codes keep their existing meaning.

If the persisted status cannot be reloaded or rendered, the original outcome and exit code are preserved and a concise status-unavailable notice is printed. A formatting problem never turns a failure into a success and never hides the primary failure. An unfamiliar or ambiguous feature name produces no run summary because there is no identified run to summarize.

## Local activity times in the dashboard

The watch dashboard labels its activity column as local time. Stored activity instants remain in UTC, but the displayed value is converted to the operating system time zone using the offset that applies to that specific instant. Daylight transitions are therefore respected. The visible format includes the date, a twenty-four-hour time with seconds and a colon-separated numeric offset, for example a date and time followed by plus eight hours.

The value is derived from the system time zone at conversion time. No fixed zone, such as Singapore, and no fixed offset is assumed. Persisted timestamps, journal ordering and elapsed-duration arithmetic are unchanged; only the display is converted.

Absent activity is shown as an em dash. A malformed or timezone-naive legacy value is shown as unavailable with an explicit unverified-timestamp note. Such a value is never silently treated as UTC and is never echoed back to the terminal. Changes to the system time zone while the dashboard is already running are out of scope; restarting the dashboard picks up the current zone.

## Platform evidence and limitations

The behavior described here was exercised on native Fedora Linux with deterministic fixtures and stub agents. The test suite covers UTC and non-UTC offsets, an offset change between two instants, absent values, malformed values, naive values, narrow and wide rendering, terminal-escape content, empty sections, and unchanged persisted evidence.

Native Windows execution was not available in this environment, so no Windows verification is claimed. The implementation uses portable time-zone conversion through the standard library and avoids Unix-only assumptions, but that is a design property, not measured Windows evidence. Redirected plain output and terminal-width detection should be rechecked by an operator on Windows before treating the presentation as certified there.

Template placeholders never appear in operator output; these tables come from persisted run facts. No new dependency, state field or contract migration was introduced for this visibility work.
