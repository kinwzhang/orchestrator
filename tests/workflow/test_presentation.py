from io import StringIO

from rich.console import Console

from orch.contracts import RunState
from orch.store import StateStore
from orch.watch import status_table


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
