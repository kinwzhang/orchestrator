"""Markdown reports generated from recorded facts, never code-quality judgments."""
from collections import defaultdict
from pathlib import Path

from orch.contracts import RunState
from orch.store import atomic_write


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
        lines.append(f"- {issue.id} ({issue.status}, root {issue.root_issue_id}, depth {issue.depth}): {issue.summary}")
        for index, assignment in enumerate(issue.assignments, 1):
            lines.append(f"  - Assignment {index}: {assignment.repairs} repairs, {assignment.failed_repairs} failed repairs, {assignment.review_rounds} review rounds.")
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
