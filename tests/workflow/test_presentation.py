from datetime import UTC, timedelta, timezone, tzinfo
from io import StringIO

import pytest
from rich.console import Console

from orch.contracts import (
    AssignmentState,
    DependencyRecord,
    FindingDependency,
    IssueState,
    LifecycleTransition,
    ReviewApplication,
    ReviewEntry,
    RunState,
)
from orch.reporting import attempt_identity, format_local_timestamp, render_status
from orch.store import StateStore
from orch.watch import format_local_activity, status_table


def test_watch_renders_untrusted_log_as_plain_text(tmp_path):
    store = StateStore(tmp_path / "state")
    log = store.run_dir("run").parent / "agent.log"
    log.parent.mkdir(parents=True)
    log.write_text("\x1b[31m[bold]agent output[/bold]\x1b[0m\n", encoding="utf-8")
    state = RunState(run_id="run", slug="demo", workers={
        "worker": {"status": "running", "log": str(log)},
    })
    store.save(state)

    table = status_table(store, "run")
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=240)
    console.print(table)
    rendered = output.getvalue()
    assert "agent output" in rendered
    assert "\x1b" not in rendered


def test_watch_shows_waiting_state_when_snapshot_is_not_yet_published(tmp_path):
    table = status_table(StateStore(tmp_path / "state"), "starting-run")
    assert "waiting for status snapshot" in table.title


def _rich_state() -> RunState:
    application = ReviewApplication(
        application_id="review-application:t1",
        transaction="review-result:t1",
        reviewer_role="code",
        attempt_id="code-reviewer-1-1",
        candidate_sha="a" * 40,
        evidence_digest="b" * 64,
        verdict_sha256="c" * 64,
        verdict_path="verdicts/code.md",
        entries=[
            ReviewEntry(entry_id="finding:bug-1", order_index=0, kind="finding",
                        issue_id="bug-1", state="applied",
                        decision={"issue": "bug-1"}),
            ReviewEntry(entry_id="finding:bug-2", order_index=1, kind="finding",
                        issue_id="bug-2"),
        ],
    )
    return RunState(
        run_id="demo-1",
        slug="demo",
        phase="RUNNING",
        stage="WORK",
        round=2,
        workers={
            "w2": {"role": "worker", "status": "running", "attempt": 3,
                   "checks": "pass", "checked": False,
                   "activity": "2026-10-31T10:38:41+00:00"},
            "planner": {"role": "planner", "status": "done", "attempt": 1,
                        "checks": "pass", "checked": True},
        },
        issues={
            "bug-1": IssueState(
                id="bug-1", worker="w1", root_issue_id="bug-1", status="in_repair",
                owner_role="code",
                summary="Repair the scope handling. " * 12,
                assignments=[AssignmentState(repairs=2, failed_repairs=1,
                                             review_rounds=2)]),
            "bug-2": IssueState(
                id="bug-2", worker="w1", root_issue_id="bug-1",
                parent_issue_id="bug-1", depth=1, status="resolved",
                legacy_status="resolved", summary="Derived follow-up."),
        },
        review_applications={application.application_id: application},
        dependency_record=DependencyRecord(
            plan_version="1", source_verdicts=["verdict:code"],
            dependencies=[FindingDependency(
                dependent_issue_id="bug-2", prerequisite_issue_ids=["bug-1"],
                rationale="Waits for the parent repair",
                shared_resource_paths=["src/orch/cli.py"],
                verification_prerequisites=["bug-1"])]),
        error="Worker is blocked by an ambiguous scope decision.",
    )


def test_render_status_preserves_semantics_and_is_read_only(tmp_path):
    state = _rich_state()
    before = state.model_dump(mode="json")
    rendered = render_status(state, plan_path="/plans/demo", run_path="/runs/demo-1",
                             width=120)
    assert state.model_dump(mode="json") == before
    flat = " ".join(rendered.split())

    assert "Run summary: demo" in rendered
    for value in ("RUNNING", "WORK", "Cycle"):
        assert value in rendered
    assert "/plans/demo" in rendered
    assert "/runs/demo-1" in rendered
    assert "Blocker: Worker is blocked by an ambiguous scope decision." in rendered

    # Deterministic ordering by identity, not insertion order.
    assert rendered.index("planner") < rendered.index("w2")
    assert rendered.index("bug-1") < rendered.index("bug-2")
    assert "not confirmed for this attempt" in rendered
    assert "done" in rendered and "pass" in rendered

    assert "bug-2" in rendered
    assert "finished (legacy resolved)" in flat
    assert "root bug-1" in flat
    assert "parent bug-1" in flat
    assert "assignment 1: 2 repairs, 1 failed, 2 review rounds" in flat
    assert "Repair the scope handling." in flat

    # Application progress stays distinct from issue lifecycle.
    assert "Review applications" in rendered
    assert "review-application:t1" in rendered
    assert "1/2" in rendered
    assert "finding:bug-2" in rendered
    assert "Dependency record" in rendered
    assert "src/orch/cli.py" in rendered

    assert "\x1b" not in rendered
    assert render_status(state, plan_path="/plans/demo", run_path="/runs/demo-1",
                         width=120) == rendered


def test_render_status_marks_finished_issues_that_are_deferred_for_a_stage():
    issue = IssueState(
        id="bug-1", worker="w1", root_issue_id="bug-1", status="finished",
        summary="Accepted with a deferred follow-up.",
        lifecycle_history=[LifecycleTransition(
            issue_id="bug-1", from_status="escalated", to_status="finished",
            guard="scoped_human_acceptance+owner_disposition+acceptance_evidence",
            evidence=["scope:remaining-work", "deferred-stage:CHECK"])])
    state = RunState(run_id="demo-1", slug="demo", phase="RUNNING", stage="WORK",
                     issues={"bug-1": issue})
    rendered = render_status(state, plan_path="/p", run_path="/r", width=120)
    flat = " ".join(rendered.split())
    assert "finished (deferred for CHECK)" in flat


def test_render_status_escapes_terminal_control_text():
    state = RunState(run_id="demo-1", slug="demo", phase="ESCALATED", stage="WORK",
                     error="bad \x1b[31mred\x07 text", workers={
                         "w1": {"status": "failed", "activity": "\x1b]0;title\x07"}})
    rendered = render_status(state, plan_path="/p", run_path="/r", width=100)
    assert "\x1b" not in rendered
    assert "\x07" not in rendered
    assert "bad red text" in rendered


def test_render_status_handles_empty_sections_and_narrow_widths():
    empty = RunState(run_id="demo-1", slug="demo", phase="DRAFT")
    rendered = render_status(empty, plan_path="/p", run_path="/r", width=100)
    assert "no agents recorded." in rendered
    assert "no issues recorded." in rendered

    state = _rich_state()
    narrow = render_status(state, plan_path="/plans/demo", run_path="/runs/demo-1",
                           width=44)
    wide = render_status(state, plan_path="/plans/demo", run_path="/runs/demo-1",
                         width=140)
    narrow_flat = " ".join(narrow.split())
    wide_flat = " ".join(wide.split())
    for token in ("bug-1", "bug-2", "in_repair", "finished", "legacy", "resolved",
                  "ambiguous", "scope"):
        assert token in narrow_flat
    for value in ("bug-1", "bug-2", "in_repair", "finished (legacy resolved)",
                  "ambiguous scope"):
        assert value in wide_flat
    assert "Agent: w2" in narrow
    assert "Issue: bug-1" in narrow


def test_render_status_compact_overview_omits_detailed_sections():
    state = _rich_state()
    rendered = render_status(state, plan_path="/plans/demo", run_path="/runs/demo-1",
                             width=100, detail=False)
    assert "Run summary: demo" in rendered
    assert "/plans/demo" in rendered
    assert "Agents" not in rendered
    assert "Issues" not in rendered
    assert "Review applications" not in rendered
    assert "Dependency record" not in rendered


class _OffsetProbe(tzinfo):
    """Deterministic local zone whose offset depends on the specific instant."""

    def utcoffset(self, value):
        if value is None:
            return timedelta(hours=1)
        return timedelta(hours=2 if 4 <= value.month <= 9 else 1)

    def dst(self, value):
        return timedelta(hours=1 if value and 4 <= value.month <= 9 else 0)

    def tzname(self, value):
        return "probe"


def test_format_local_activity_uses_utc_and_non_utc_zones(monkeypatch):
    from orch import watch

    monkeypatch.setattr(watch, "_localize", lambda moment: moment.astimezone(UTC))
    assert format_local_activity("2026-10-31T10:38:41.981586+00:00") == \
        "2026-10-31 10:38:41+00:00"

    monkeypatch.setattr(watch, "_localize",
                        lambda moment: moment.astimezone(timezone(timedelta(hours=8))))
    assert format_local_activity("2026-10-31T10:38:41.981586+00:00") == \
        "2026-10-31 18:38:41+08:00"


def test_format_local_activity_uses_offset_for_the_specific_instant(monkeypatch):
    from orch import watch

    monkeypatch.setattr(watch, "_localize", lambda moment: moment.astimezone(_OffsetProbe()))
    assert format_local_activity("2026-01-15T12:00:00+00:00") == \
        "2026-01-15 13:00:00+01:00"
    assert format_local_activity("2026-07-15T12:00:00+00:00") == \
        "2026-07-15 14:00:00+02:00"


def test_format_local_activity_falls_back_without_assuming_utc(monkeypatch):
    from orch import watch

    monkeypatch.setattr(watch, "_localize", lambda moment: moment.astimezone(UTC))
    assert format_local_activity(None) == "—"
    assert format_local_activity("") == "—"
    assert format_local_activity("not-a-timestamp") == "unavailable (unverified timestamp)"
    assert format_local_activity("2026-10-31T10:38:41") == \
        "unavailable (unverified timestamp)"
    assert format_local_activity("2026-13-45T99:99:99+00:00") == \
        "unavailable (unverified timestamp)"


def test_watch_table_labels_local_activity_and_keeps_persisted_evidence(tmp_path, monkeypatch):
    from orch import watch

    monkeypatch.setattr(watch, "_localize",
                        lambda moment: moment.astimezone(timezone(timedelta(hours=8))))
    store = StateStore(tmp_path / "state")
    state = RunState(run_id="run", slug="demo", workers={
        "w1": {"status": "done", "attempt": 1,
               "activity": "2026-10-31T10:38:41.981586+00:00"}})
    store.save(state)
    snapshot = (store.run_dir("run") / "state.json").read_bytes()

    output = StringIO()
    Console(file=output, force_terminal=False, width=240).print(status_table(store, "run"))
    rendered = output.getvalue()
    assert "Last activity (local)" in rendered
    assert "2026-10-31 18:38:41+08:00" in rendered
    assert (store.run_dir("run") / "state.json").read_bytes() == snapshot
    assert store.load("run").workers["w1"]["activity"] == \
        "2026-10-31T10:38:41.981586+00:00"


def _invocation_state(*, conflicting=False, legacy=False) -> RunState:
    attempts = {
        "w1-1-1": {
            "logical_id": "w1", "attempt_id": "w1-1-1", "adapter": "opencode",
            "provider": "alpha", "model": "alpha/one",
            "evidence_status": "recorded",
        },
        "w1-1-2": {
            "logical_id": "w1", "attempt_id": "w1-1-2", "adapter": "codex",
            "provider": "codex", "model": "gpt-second",
            "evidence_status": "conflicting" if conflicting else "recorded",
        },
    }
    worker = {"role": "worker", "status": "running", "attempt": 2,
              "attempt_id": "w1-1-2", "started_at": "2026-10-31T10:38:41+00:00",
              "activity": "2026-10-31T10:40:41+00:00",
              "checks": "pass", "checked": False}
    if not legacy:
        worker["invocation_attempts"] = attempts
    return RunState(run_id="demo-1", slug="demo", phase="RUNNING", stage="WORK",
                    started_at="2026-10-31T09:00:00+00:00",
                    updated_at="2026-10-31T10:40:41+00:00",
                    workers={"w1": worker})


def _agent_row(rendered: str, agent: str) -> str:
    rows = [line for line in rendered.splitlines()
            if line.startswith("│") and f" {agent} " in line]
    assert rows, rendered
    return rows[0]


def test_status_shows_the_current_attempt_invocation_in_the_agent_row(monkeypatch):
    from orch import reporting

    monkeypatch.setattr(reporting, "_localize", lambda moment: moment.astimezone(UTC))
    state = _invocation_state()
    before = state.model_dump(mode="json")
    rendered = render_status(state, plan_path="/p", run_path="/r", width=160)
    flat = " ".join(rendered.split())

    # The row describes the current attempt only, from captured evidence.
    row = _agent_row(rendered, "w1")
    assert "codex" in row and "gpt-second" in row
    assert "alpha/one" not in row
    # Retained historical attempts are labeled separately.
    assert "Attempt invocations" in flat
    history = "\n".join(rendered.splitlines()[rendered.splitlines().index(
        "Attempt invocations (recorded at the adapter boundary; current preferences are never used)"):])
    assert "w1-1-1" in history and "alpha" in history and "alpha/one" in history
    assert "w1-1-2" in history and "gpt-second" in history
    assert state.model_dump(mode="json") == before


def test_status_never_invents_identity_from_legacy_or_conflicting_evidence(monkeypatch):
    from orch import reporting

    monkeypatch.setattr(reporting, "_localize", lambda moment: moment.astimezone(UTC))
    legacy = render_status(_invocation_state(legacy=True),
                           plan_path="/p", run_path="/r", width=160)
    assert "unknown" in _agent_row(legacy, "w1")
    # A legacy current attempt is listed, but only with unknown identity.
    assert "Attempt invocations" in legacy
    assert "alpha/one" not in legacy

    conflicting = render_status(_invocation_state(conflicting=True),
                                plan_path="/p", run_path="/r", width=160)
    assert "unknown" in _agent_row(conflicting, "w1")
    assert "conflicting (shown as unknown)" in conflicting
    # Historical recorded attempts remain visible; only the conflict is unknown.
    assert "alpha/one" in conflicting


def test_attempt_identity_reads_only_recorded_evidence():
    info = {"attempt_id": "w1-1-2", "invocation_attempts": {
        "w1-1-1": {"logical_id": "w1", "attempt_id": "w1-1-1",
                   "adapter": "opencode", "provider": "alpha",
                   "model": "alpha/one", "evidence_status": "recorded"},
        "w1-1-2": {"logical_id": "w1", "attempt_id": "w1-1-2",
                   "adapter": "opencode", "provider": "alpha",
                   "model": "alpha/one", "evidence_status": "conflicting"},
    }}
    assert attempt_identity(info, "w1-1-1") == ("alpha", "alpha/one")
    assert attempt_identity(info, "w1-1-2") == ("unknown", "unknown")
    assert attempt_identity(info, "w1-1-9") == ("unknown", "unknown")
    assert attempt_identity({}, "w1-1-1") == ("unknown", "unknown")
    mismatched = {"invocation_attempts": {"w1-1-1": {
        "attempt_id": "w1-1-2", "provider": "alpha", "model": "alpha/one",
        "evidence_status": "recorded"}}}
    assert attempt_identity(mismatched, "w1-1-1") == ("unknown", "unknown")
    partial = {"invocation_attempts": {"w1-1-1": {
        "attempt_id": "w1-1-1", "provider": "alpha",
        "evidence_status": "recorded"}}}
    assert attempt_identity(partial, "w1-1-1") == ("alpha", "unknown")


def test_compact_status_shows_attempts_only_when_evidence_exists(monkeypatch):
    from orch import reporting

    monkeypatch.setattr(reporting, "_localize", lambda moment: moment.astimezone(UTC))
    with_attempts = render_status(_invocation_state(), plan_path="/p", run_path="/r",
                                  width=120, detail=False)
    assert "Attempt invocations" in with_attempts
    assert "alpha/one" in with_attempts and "gpt-second" in with_attempts
    assert "Issues" not in with_attempts

    without = render_status(_rich_state(), plan_path="/p", run_path="/r",
                            width=120, detail=False)
    assert "Attempt invocations" not in without


def test_status_timestamps_use_system_local_time_with_numeric_offset(monkeypatch):
    from orch import reporting

    monkeypatch.setattr(reporting, "_localize", lambda moment: moment.astimezone(UTC))
    rendered = render_status(_invocation_state(), plan_path="/p", run_path="/r", width=160)
    assert "Run started (local)" in rendered
    assert "2026-10-31 09:00:00+00:00" in rendered
    assert "2026-10-31 10:40:41+00:00" in rendered

    monkeypatch.setattr(reporting, "_localize",
                        lambda moment: moment.astimezone(timezone(timedelta(hours=8))))
    rendered = render_status(_invocation_state(), plan_path="/p", run_path="/r", width=160)
    assert "2026-10-31 18:40:41+08:00" in rendered


def test_status_timestamp_fallbacks_never_assume_utc(monkeypatch):
    from orch import reporting

    monkeypatch.setattr(reporting, "_localize", lambda moment: moment.astimezone(UTC))
    state = RunState(
        run_id="demo-1", slug="demo", phase="RUNNING", stage="WORK",
        started_at="2026-10-31T09:00:00", updated_at="not-a-timestamp",
        workers={"w1": {"status": "running", "activity": None}})
    rendered = render_status(state, plan_path="/p", run_path="/r", width=120)
    assert "unavailable (unverified timestamp)" in rendered
    assert "2026-10-31 09:00:00" not in rendered
    assert "—" in rendered


def test_watch_shows_attempt_invocation_and_local_activity(tmp_path, monkeypatch):
    from orch import watch

    monkeypatch.setattr(watch, "_localize",
                        lambda moment: moment.astimezone(timezone(timedelta(hours=8))))
    store = StateStore(tmp_path / "state")
    store.save(_invocation_state())
    snapshot = (store.run_dir("demo-1") / "state.json").read_bytes()

    output = StringIO()
    Console(file=output, force_terminal=False, width=260).print(
        status_table(store, "demo-1"))
    rendered = output.getvalue()
    assert "Provider" in rendered and "Model" in rendered
    assert "codex" in rendered and "gpt-second" in rendered
    assert "2026-10-31 18:40:41+08:00" in rendered
    assert (store.run_dir("demo-1") / "state.json").read_bytes() == snapshot


def test_status_ignores_preference_fields_and_covers_reviewer_attempts():
    state = RunState(run_id="demo-1", slug="demo", phase="RUNNING", stage="WORK", workers={
        "w1": {"role": "worker", "status": "done", "attempt": 1,
               "attempt_id": "w1-1-1", "model": "preferred/other",
               "invocation_attempts": {"w1-1-1": {
                   "logical_id": "w1", "attempt_id": "w1-1-1", "adapter": "opencode",
                   "provider": "alpha", "model": "alpha/one",
                   "evidence_status": "recorded"}}},
        "code-reviewer": {"role": "code_reviewer", "status": "done", "attempt": 1,
                          "attempt_id": "code-reviewer-1-1",
                          "invocation_attempts": {"code-reviewer-1-1": {
                              "logical_id": "code-reviewer",
                              "attempt_id": "code-reviewer-1-1", "adapter": "opencode",
                              "provider": "gamma", "model": "gamma/reviewer",
                              "evidence_status": "recorded"}}},
    })
    rendered = render_status(state, plan_path="/p", run_path="/r", width=160)
    assert "preferred/other" not in rendered
    assert "alpha/one" in _agent_row(rendered, "w1")
    assert "gamma/reviewer" in _agent_row(rendered, "code-reviewer")


@pytest.mark.parametrize("status", ["running", "done", "failed", "interrupted"])
def test_retained_evidence_survives_every_attempt_outcome(status):
    state = RunState(run_id="demo-1", slug="demo", phase="RUNNING", stage="WORK", workers={
        "w1": {"role": "worker", "status": status, "attempt": 1,
               "attempt_id": "w1-1-1",
               "invocation_attempts": {"w1-1-1": {
                   "logical_id": "w1", "attempt_id": "w1-1-1", "adapter": "opencode",
                   "provider": "alpha", "model": "alpha/one",
                   "evidence_status": "recorded"}}},
    })
    rendered = render_status(state, plan_path="/p", run_path="/r", width=160)
    assert "alpha/one" in _agent_row(rendered, "w1")


def test_format_local_timestamp_is_generic(monkeypatch):
    from orch import reporting

    monkeypatch.setattr(reporting, "_localize",
                        lambda moment: moment.astimezone(_OffsetProbe()))
    assert format_local_timestamp("2026-01-15T12:00:00+00:00") == \
        "2026-01-15 13:00:00+01:00"
    assert format_local_timestamp("2026-07-15T12:00:00+00:00") == \
        "2026-07-15 14:00:00+02:00"
    assert format_local_timestamp(None) == "—"
    assert format_local_timestamp("2026-07-15T12:00:00") == \
        "unavailable (unverified timestamp)"
