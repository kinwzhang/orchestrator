# Operator visibility: status tables, next summaries, local times and attempt provenance

This guide explains what the operator sees when inspecting a run with the status command, when asking the orchestrator to take the next step, and when watching a running dashboard. It describes meaning and behavior only. It does not require reading source files.

## Status at a glance

The status command answers four questions: which feature and run is being shown, where the delivery has reached, what should happen next, and whether a person must act.

The run summary table always shows the feature slug, the current lifecycle phase, the delivery stage, the delivery cycle number and the next action. It also shows when the run started and when it was last updated, both converted to system-local time with a visible numeric offset. The plan folder path and the run folder path follow. The plan path is where the approved planning documents live. The run path is where the orchestrator keeps state, evidence, handovers, verdicts and the Markdown checklist, activity log and issue register.

When the stored run records a blocker, a blocker line appears above the summary table so it is the first thing read. A blocker is a condition the orchestrator cannot resolve on its own, such as a failed check, an uncertain process, an approval change or an exhausted allowance. When the next action is a human gate, the summary also shows a human action row. That row states the kind of decision required: review and approve a plan, test the feature and accept it, resume a paused or escalated run, or recognize that the run is complete or aborted.

Status never changes the run. It does not advance a stage, dispatch an agent, write state or alter approval. Rendering reads the already-loaded snapshot and returns text. The same is true of watch and of the status printed after the next command; display never rewrites stored timestamps or invocation evidence.

## The agent table

The agent table lists every recorded role in a stable name order. Each row shows the agent or role name, its role, its status, its attempt number, the provider and model selected for that attempt, its checks and its last recorded activity in local time.

The attempt number identifies which dispatch the displayed information belongs to. The invocation column is the provider and model actually selected when that attempt's command was built, not a current preference or a role default. When the recorded evidence does not establish the provider or the model, the column says unknown rather than guessing. A known model can still appear when only its provider is unqualified.

Checks are deliberately conservative. A check result is shown only when completion for the current attempt was recorded. If an older check value remains in state while a newer attempt has not yet been confirmed, the table says the checks are not confirmed for this attempt instead of implying that the old result still applies. This prevents a successful earlier attempt from looking like a successful current attempt.

Raw agent logs are not placed in the status table. Log tails and live output remain the responsibility of the watch dashboard and detailed run evidence.

## Attempt invocation history

Below the agent table, a clearly labeled attempt invocation section records the provider, model, adapter and evidence condition for each attempt that the run retained. It includes the current attempt even when the only thing known about it is its identity. Historical attempts stay visible after later dispatches, so an operator can see that a first attempt used one provider and model and a replacement or retry used another.

This evidence is captured when the adapter has built the real command and is about to hand it to the process runner. It describes the selected provider and model identity for that attempt. It is not proof of provider-side routing beyond that selection, and it is never reconstructed from present-day preferences, display names or an unexecuted dispatch intent.

If the same attempt reports identical evidence twice, the record stays unchanged. If it reports conflicting evidence, the conflict is retained and the affected identity is shown as unknown with an explicit conflicting note. Legacy runs and attempts that failed before the capture point simply show unknown. Historical counters, attempt identity and reviewer history are not renamed, merged or backfilled.

The compact status overview, used when status is called without a feature name, also shows the attempt invocation section whenever retained attempts exist; otherwise it stays a short run summary.

## Local timestamps on every surface

Every structured timestamp shown by status, watch and the summary after the next command uses system-local time with a colon-separated numeric offset and a time of day that includes seconds. This covers run start and update times and per-agent activity times. Free-form agent output is not a structured timestamp and is never reinterpreted as one.

Stored timestamps remain in UTC and in their original form. Display converts each instant using the system zone and the offset that applies to that specific instant, including supported daylight transitions. No launch-time zone is hardcoded, and no fixed offset is assumed. Ordering and duration arithmetic keep using the stored values.

An absent timestamp displays an em dash. A malformed or timezone-naive legacy value displays unavailable with an explicit unverified-timestamp note. Such a value is never silently treated as UTC and is never echoed back to the terminal. A time-zone change while the watch dashboard is already running is out of scope; restarting the dashboard picks up the current zone.

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

After the next command finishes a stage, stops at a human gate, or handles a failure or escalation, it prints exactly one current status. The status is the same detailed presentation used by the status command with a feature name, including local run times, per-attempt invocation evidence and the historical attempt section. This means the operator does not need a second command to understand the resulting state.

The summary is produced from the persisted run after handling and reconciliation, not from an earlier in-memory copy. Printing it does not advance another stage, dispatch another agent or change lifecycle behavior. Explanatory messages, such as a human gate notice or the primary error, are retained. Exit codes keep their existing meaning.

If the persisted status cannot be reloaded or rendered, the original outcome and exit code are preserved and a concise status-unavailable notice is printed. A formatting problem never turns a failure into a success and never hides the primary failure. An unfamiliar or ambiguous feature name produces no run summary because there is no identified run to summarize.

## Watch timestamps and invocation columns

The watch dashboard labels its activity column as local time and also shows the provider and model captured for each agent's current attempt. The same local-time rules apply: the displayed value uses the system zone and the offset for that specific instant, with seconds and a numeric offset. Stored values stay unchanged.

Absent activity shows an em dash. A malformed or timezone-naive legacy value shows unavailable with an explicit unverified-timestamp note. Identity that was never captured shows unknown in its provider or model column instead of borrowing present-day preferences. Agent output text in the dashboard is sanitized plain text and is not treated as a timestamp.

## Platform evidence and limitations

The behavior described here was exercised on native Fedora Linux with deterministic fixtures and stub agents. The test suite covers UTC and non-UTC offsets, an offset change between two instants, absent values, malformed values, naive values, narrow and wide rendering, terminal-escape content, empty sections, unchanged persisted state and journal evidence, two attempts for one agent with different providers and models, a later preference change that must not affect the display, replacement and reviewer attempts, running, completed, failed and interrupted attempts, launch prevented before capture, and absent, partial or conflicting legacy evidence.

Native Windows execution was not available in this environment, so no Windows verification is claimed. The implementation uses portable time-zone conversion through the standard library and avoids Unix-only assumptions, but that is a design property, not measured Windows evidence. Redirected plain output and terminal-width detection should be rechecked by an operator on Windows before treating the presentation as certified there.

Template placeholders never appear in operator output; these tables come from persisted run facts. No new dependency, contract migration or workflow lifecycle change was introduced for this visibility work. The only added durable data is per-attempt adapter invocation evidence, which is additive metadata on existing worker records and journal events.
