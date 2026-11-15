"""End-to-end operator visibility for orch status and orch next."""

from types import SimpleNamespace

import pytest

from orch.cli import main
from orch.contracts import RunState
from orch.store import StateStore


def _run(tmp_path, monkeypatch, phase, *, stage="WORK", status="ready"):
    store = StateStore(tmp_path / "state")
    state = RunState(run_id="demo-1", slug="demo", phase=phase, stage=stage)
    store.save(state)
    entry = {"slug": "demo", "run_id": "demo-1", "repo": str(tmp_path),
             "plan": str(tmp_path / "plan"), "status": status}
    monkeypatch.setattr("orch.cli.registry", lambda _store: [entry])
    monkeypatch.setattr(
        "orch.cli.context",
        lambda _store, _slug, global_models=False: (
            entry, None, store.load("demo-1"),
            SimpleNamespace(config=SimpleNamespace(viewer=False))))
    return store, state, entry


def _invoke(store, command, slug="demo"):
    return main(["--data-dir", str(store.data_dir), command, slug])


def test_next_prints_one_status_after_a_human_gate(tmp_path, monkeypatch, capsys):
    store, _, _ = _run(tmp_path, monkeypatch, "PLAN_READY")

    assert _invoke(store, "next") == 0
    captured = capsys.readouterr()
    assert "Human gate: APPROVE" in captured.out
    assert captured.out.count("Run summary: demo") == 1
    assert "Next action" in captured.out
    assert "Human action" in captured.out
    assert store.load("demo-1").phase == "PLAN_READY"


def test_next_prints_one_status_after_a_successful_stage(tmp_path, monkeypatch, capsys):
    store, _, _ = _run(tmp_path, monkeypatch, "APPROVED")
    calls = []

    class StubEngine:
        async def run(self, one_stage=False):
            calls.append(one_stage)
            current = store.load("demo-1")
            current.stage = "CHECK"
            store.save(current)

    monkeypatch.setattr("orch.cli.engine",
                        lambda *_args, **_kwargs: StubEngine())

    assert _invoke(store, "next") == 0
    captured = capsys.readouterr()
    assert calls == [True]
    assert store.load("demo-1").stage == "CHECK"
    assert captured.out.count("Run summary: demo") == 1
    assert "CHECK" in captured.out


def test_next_keeps_failure_and_prints_persisted_status(tmp_path, monkeypatch, capsys):
    store, _, _ = _run(tmp_path, monkeypatch, "APPROVED")

    class FailingEngine:
        async def run(self, one_stage=False):
            current = store.load("demo-1")
            current.phase = "ESCALATED"
            current.error = "simulated stage failure"
            store.save(current)
            raise RuntimeError("simulated stage failure")

    monkeypatch.setattr("orch.cli.engine",
                        lambda *_args, **_kwargs: FailingEngine())

    assert _invoke(store, "next") == 1
    captured = capsys.readouterr()
    assert "simulated stage failure" in captured.err
    assert captured.out.count("Run summary: demo") == 1
    assert "Blocker: simulated stage failure" in captured.out
    assert "ESCALATED" in captured.out
    assert store.load("demo-1").phase == "ESCALATED"


def test_next_secondary_render_failure_preserves_primary_error(
        tmp_path, monkeypatch, capsys):
    store, _, _ = _run(tmp_path, monkeypatch, "APPROVED")

    class FailingEngine:
        async def run(self, one_stage=False):
            raise RuntimeError("primary failure")

    monkeypatch.setattr("orch.cli.engine",
                        lambda *_args, **_kwargs: FailingEngine())
    monkeypatch.setattr("orch.cli.render_status",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            RuntimeError("render unavailable")))

    assert _invoke(store, "next") == 1
    captured = capsys.readouterr()
    assert "primary failure" in captured.err
    assert "Status unavailable" in captured.err
    assert "Run summary: demo" not in captured.out
    assert store.load("demo-1").phase == "APPROVED"


def test_next_for_an_unidentified_run_prints_no_summary(
        tmp_path, monkeypatch, capsys):
    store = StateStore(tmp_path / "state")
    monkeypatch.setattr("orch.cli.registry", lambda _store: [])

    assert _invoke(store, "next") == 1
    captured = capsys.readouterr()
    assert "Run summary" not in captured.out
    assert "Unknown or ambiguous feature" in captured.err


def test_status_with_slug_uses_the_detailed_view(tmp_path, monkeypatch, capsys):
    store, _, _ = _run(tmp_path, monkeypatch, "RUNNING")

    assert _invoke(store, "status") == 0
    captured = capsys.readouterr()
    assert "Run summary: demo" in captured.out
    assert "Agents" in captured.out
    assert "Issues" in captured.out
    assert "no agents recorded." in captured.out
    assert "no issues recorded." in captured.out


def test_status_without_slug_keeps_compact_overview_and_creation_entries(
        tmp_path, monkeypatch, capsys):
    store, _, entry = _run(tmp_path, monkeypatch, "RUNNING")
    creating = {"slug": "unfinished", "run_id": "unfinished-1",
                "repo": str(tmp_path), "plan": str(tmp_path / "plan"),
                "status": "creating"}
    monkeypatch.setattr("orch.cli.registry", lambda _store: [entry, creating])

    assert _invoke(store, "status", slug=None) == 0
    captured = capsys.readouterr()
    assert "Run summary: demo" in captured.out
    assert "Agents" not in captured.out
    assert "Issues" not in captured.out
    assert "unfinished: creation interrupted" in captured.out


@pytest.mark.parametrize("width", [40, 100])
def test_status_stays_readable_and_read_only_at_terminal_widths(
        tmp_path, monkeypatch, capsys, width):
    store, _, _ = _run(tmp_path, monkeypatch, "RUNNING")
    snapshot = (store.run_dir("demo-1") / "state.json").read_bytes()
    monkeypatch.setattr("orch.cli.console_width", lambda default=100: width)

    assert _invoke(store, "status") == 0
    rendered = capsys.readouterr().out
    assert "Run summary: demo" in rendered
    assert "\x1b" not in rendered
    assert (store.run_dir("demo-1") / "state.json").read_bytes() == snapshot
