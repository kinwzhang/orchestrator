"""Markdown reports generated from recorded facts, never code-quality judgments."""
import re
from collections import defaultdict
from io import StringIO
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

from orch.contracts import RunState
from orch.store import atomic_write

# Untrusted values may reach a terminal through state, handovers or summaries.
# Strip ANSI/OSC/control sequences and always render through literal Text so
# Rich markup in stored content is never interpreted.
_OSC_SEQUENCE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_ANSI_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SHORT_SUMMARY_LIMIT = 100


def _safe_text(value) -> str:
    text = "" if value is None else str(value)
    text = _OSC_SEQUENCE.sub("", text)
    text = _ANSI_SEQUENCE.sub("", text)
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32 and ord(c) != 127)


def _short(value, limit: int = _SHORT_SUMMARY_LIMIT) -> str:
    text = " ".join(_safe_text(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _table(title: str, columns, rows) -> Table:
    table = Table(title=Text(_safe_text(title)), show_lines=False)
    for column in columns:
        table.add_column(_safe_text(column))
    for row in rows:
        table.add_row(*(Text(_safe_text(value)) for value in row))
    return table


def _field_table(title: str, pairs) -> Table:
    return _table(title, ("Field", "Value"), pairs)


def _render(parts, width: int) -> str:
    buffer = StringIO()
    console = Console(file=buffer, width=max(20, int(width)),
                      force_terminal=False, color_system=None, highlight=False)
    for part in parts:
        console.print(part)
    return buffer.getvalue().rstrip("\n")


_HUMAN_ACTIONS = {
    "APPROVE": "Review the plan, then run orch approve",
    "HUMAN_TEST": "Test the feature branch, then run orch done",
    "RESUME": "Review the blocker or pause, then run orch resume",
    "NONE": "No further action; the run is complete or aborted",
}


def _checks_text(info: dict) -> str:
    """Only claim checks for the current attempt when completion was recorded."""
    checks = info.get("checks")
    if info.get("checked") is True:
        return _safe_text(checks) if checks not in (None, "") else "recorded"
    if checks not in (None, ""):
        return "not confirmed for this attempt"
    return "pending"


def _worker_rows(state: RunState):
    rows = []
    for name in sorted(state.workers):
        info = state.workers[name]
        rows.append((
            name,
            info.get("role", "unrecorded"),
            info.get("status", "pending"),
            info.get("attempt", 0),
            _checks_text(info),
            info.get("activity", "not recorded"),
        ))
    return rows


def _dependency_lookup(state: RunState) -> dict:
    if state.dependency_record is None:
        return {}
    return {item.dependent_issue_id: item for item in state.dependency_record.dependencies}


def _deferred_stages(issue) -> list[str]:
    stages = set()
    for transition in issue.lifecycle_history:
        for evidence in transition.evidence:
            if evidence.startswith("deferred-stage:"):
                stages.add(evidence.split(":", 1)[1])
    return sorted(stages)


def _issue_rows(state: RunState, dependencies: dict):
    rows, details, long_summaries = [], [], []
    for identity in sorted(state.issues):
        issue = state.issues[identity]
        legacy = f" (legacy {issue.legacy_status})" if issue.legacy_status else ""
        deferred = _deferred_stages(issue)
        deferral = f" (deferred for {', '.join(deferred)})" if deferred else ""
        summary = " ".join(_safe_text(issue.summary).split())
        if len(summary) > _SHORT_SUMMARY_LIMIT:
            long_summaries.append((identity, summary))
        rows.append((
            identity,
            f"{issue.lifecycle}{legacy}{deferral}",
            issue.attempt,
            issue.reassignments,
            issue.worker,
            _short(summary) or "no summary recorded",
        ))
        dependency = dependencies.get(identity)
        waits = []
        if dependency is not None:
            waits = sorted(set(dependency.prerequisite_issue_ids)
                           | set(dependency.verification_prerequisites))
        assignments = []
        for number, assignment in enumerate(issue.assignments, start=1):
            assignments.append(
                f"assignment {number}: {assignment.repairs} repairs, "
                f"{assignment.failed_repairs} failed, {assignment.review_rounds} review rounds")
        parts = [
            f"root {issue.root_issue_id}",
            f"parent {issue.parent_issue_id or 'none'}",
            f"depth {issue.depth}",
            f"owner role {issue.owner_role or 'unrecorded'}",
            f"waits on {', '.join(waits) if waits else 'nothing declared'}",
            "; ".join(assignments) if assignments else "no assignments recorded",
        ]
        details.append(f"- {identity}: " + "; ".join(parts))
    return rows, details, long_summaries


def _application_rows(state: RunState):
    rows, gaps = [], []
    for identity in sorted(state.review_applications):
        application = state.review_applications[identity]
        state_label = "complete" if application.complete else "pending"
        rows.append((
            identity,
            application.reviewer_role,
            f"{len(application.applied_entry_ids)}/{len(application.entries)}",
            application.next_entry_index,
            state_label,
        ))
        for entry in application.entries:
            if entry.state != "applied":
                gaps.append(
                    f"- {identity}: waiting to apply entry {entry.entry_id} "
                    f"at position {entry.order_index}")
    return rows, gaps


def _dependency_rows(state: RunState):
    record = state.dependency_record
    if record is None:
        return []
    rows = []
    for item in sorted(record.dependencies, key=lambda entry: entry.dependent_issue_id):
        waits = sorted(set(item.prerequisite_issue_ids) | set(item.verification_prerequisites))
        rows.append((
            item.dependent_issue_id,
            ", ".join(waits) if waits else "nothing declared",
            ", ".join(item.shared_resource_paths) if item.shared_resource_paths else "none",
            ", ".join(item.verification_prerequisites) if item.verification_prerequisites
            else "none",
        ))
    return rows


def _render_agents(state: RunState, width: int):
    rows = _worker_rows(state)
    if not rows:
        return [Text("Agents: no agents recorded.")]
    columns = ("Agent", "Role", "Status", "Attempt", "Checks", "Activity")
    if width < 72:
        parts = [Text("Agents", style="bold")]
        for row in rows:
            parts.append(_field_table(f"Agent: {row[0]}", zip(columns[1:], row[1:])))
        return parts
    return [Text("Agents", style="bold"), _table("", columns, rows)]


def _render_issues(state: RunState, width: int):
    dependencies = _dependency_lookup(state)
    rows, details, long_summaries = _issue_rows(state, dependencies)
    if not rows:
        return [Text("Issues: no issues recorded.")]
    columns = ("ID", "Lifecycle", "Attempt", "Reassignments", "Owner", "Summary")
    if width < 100:
        parts = [Text("Issues", style="bold")]
        for row in rows:
            parts.append(_field_table(f"Issue: {row[0]}", zip(columns[1:], row[1:])))
    else:
        parts = [Text("Issues", style="bold"), _table("", columns, rows)]
    if details:
        parts.append(Text("Issue details", style="bold"))
        parts.extend(Text(line) for line in details)
    if long_summaries:
        parts.append(Text("Full issue summaries", style="bold"))
        parts.extend(Text(f"- {identity}: {summary}")
                     for identity, summary in long_summaries)
    return parts


def _render_applications(state: RunState, width: int):
    rows, gaps = _application_rows(state)
    if not rows:
        return []
    columns = ("Application", "Role", "Applied", "Next entry", "State")
    parts = [Text("Review applications (application progress is separate from issue lifecycle)",
                  style="bold")]
    if width < 100:
        for row in rows:
            parts.append(_field_table(f"Application: {row[0]}", zip(columns[1:], row[1:])))
    else:
        parts.append(_table("", columns, rows))
    if gaps:
        parts.append(Text("Pending application entries", style="bold"))
        parts.extend(Text(line) for line in gaps)
    return parts


def _render_dependencies(state: RunState, width: int):
    rows = _dependency_rows(state)
    if not rows:
        return []
    columns = ("Dependent issue", "Waits on", "Shared paths", "Verification waits")
    parts = [Text("Dependency record", style="bold")]
    if width < 100:
        for row in rows:
            parts.append(_field_table(f"Dependent issue: {row[0]}", zip(columns[1:], row[1:])))
    else:
        parts.append(_table("", columns, rows))
    return parts


def render_status(state: RunState, *, plan_path, run_path, width: int = 100,
                  detail: bool = True) -> str:
    """Render one already-loaded run as terminal-safe plain text.

    Reads nothing and mutates nothing. ``detail`` selects the full operator view
    with agent, issue and application sections; the compact view is the run
    summary used for the no-slug multi-run overview.
    """
    from orch.workflow import next_action

    action = next_action(state)
    parts = []
    if state.error:
        parts.append(Text(_safe_text(f"Blocker: {state.error}")))
    summary = [
        ("Slug", state.slug),
        ("Phase", state.phase),
        ("Stage", state.stage),
        ("Cycle", state.round),
        ("Next action", action),
    ]
    human_action = _HUMAN_ACTIONS.get(action)
    if human_action:
        summary.append(("Human action", human_action))
    summary.extend((("Plan", plan_path), ("Run", run_path)))
    parts.append(_field_table(f"Run summary: {state.slug}", summary))
    if detail:
        parts.append(Text(""))
        parts.extend(_render_agents(state, width))
        parts.append(Text(""))
        parts.extend(_render_issues(state, width))
        applications = _render_applications(state, width)
        if applications:
            parts.append(Text(""))
            parts.extend(applications)
        dependencies = _render_dependencies(state, width)
        if dependencies:
            parts.append(Text(""))
            parts.extend(dependencies)
    return _render(parts, width)


def _metrics(value: dict, prefix: str = ""):
    for name, item in value.items():
        key = f"{prefix}.{name}" if prefix else name
        if isinstance(item, dict):
            yield from _metrics(item, key)
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            yield key, item


def render_report(state: RunState, events: list[dict]) -> str:
    calls = [e for e in events if e.get("kind") == "agent_completed"]
    seconds = sum(e.get("duration_seconds", 0) for e in calls)
    totals = defaultdict(float)
    exposed = 0
    for call in calls:
        usage = call.get("usage") or {}
        if usage:
            exposed += 1
        for name, value in _metrics(usage):
            totals[name] += value
    lines = [f"# Feature report: {state.slug}", "", f"Phase: {state.phase}",
             f"Stage: {state.stage}; cycle: {state.round}", "",
             f"Candidate: {state.candidate.get('sha', 'not assembled')}",
             f"Agent calls: {len(calls)}; recorded agent time: {seconds:.2f} seconds.",
             f"Usage exposed for {exposed}/{len(calls)} calls; unknown usage is not zero.", ""]
    if totals:
        lines += ["## Recorded usage totals", "", "These sum matching metric names only; provider units may differ.", ""]
        lines += [f"- {name}: {value:g}" for name, value in sorted(totals.items())]
        lines.append("")
    if state.error:
        lines += ["## Blocker", "", state.error, ""]
    lines += ["## Assignments", ""]
    for name, info in state.workers.items():
        lines.append(f"- {name}: {info.get('status', 'pending')}; handover: {info.get('handover', 'none')}")
    lines += ["", "## Issues", ""]
    for issue in state.issues.values():
        legacy = f"; legacy {issue.legacy_status}" if issue.legacy_status else ""
        lines.append(
            f"- {issue.id} ({issue.lifecycle}{legacy}, root {issue.root_issue_id}, "
            f"depth {issue.depth}): {issue.summary}")
        for index, assignment in enumerate(issue.assignments, 1):
            lines.append(f"  - Assignment {index}: {assignment.repairs} repairs, {assignment.failed_repairs} failed repairs, {assignment.review_rounds} review rounds.")
    if state.review_applications:
        lines += ["", "## Review applications", ""]
        for application in state.review_applications.values():
            lines.append(
                f"- {application.application_id} ({application.reviewer_role}, "
                f"transaction {application.transaction}): "
                f"{len(application.applied_entry_ids)}/{len(application.entries)} entries "
                f"applied; next {application.next_entry_index}; "
                f"{'complete' if application.complete else 'incomplete'}.")
    if state.dependency_record is not None:
        lines += ["", "## Dependency record", ""]
        lines.append(
            f"- plan {state.dependency_record.plan_version}; "
            f"{len(state.dependency_record.dependencies)} dependent issue(s).")
        for item in state.dependency_record.dependencies:
            waits = sorted(set(item.prerequisite_issue_ids)
                           | set(item.verification_prerequisites))
            lines.append(
                f"  - {item.dependent_issue_id}: waits on "
                f"{', '.join(waits) if waits else 'nothing declared'}"
                + (f"; shared {', '.join(item.shared_resource_paths)}"
                   if item.shared_resource_paths else ""))
    lines += ["", "## Usage by call", ""]
    for call in calls:
        lines.append(f"- {call.get('agent')} ({call.get('model', 'unknown model')}): {call.get('duration_seconds', 0):.2f}s; usage: {call.get('usage') or 'unavailable'}")
    lines += ["", "## Human test and evidence", "",
              "Read the code review verdict's Manual test notes and test the feature branch.",
              "See checklist.md, issues.md and activity_log.md in the run directory for dispatches, reviews, rework and recovery.",
              "Only the feature branch is a permitted promotion target. Human testing is required before closing the feature.", ""]
    return "\n".join(lines)


def write_report(state: RunState, events: list[dict], run_dir: Path, plan: Path) -> None:
    report = render_report(state, events)
    atomic_write(run_dir / "REPORT.md", report)
    atomic_write(plan / "report.md", report)
