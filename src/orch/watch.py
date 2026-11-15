"""Read-only terminal view; status is loaded from disk on every refresh."""
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from orch.reporting import attempt_identity, format_local_timestamp
from orch.store import StateStore


def _localize(moment: datetime) -> datetime:
    """Conversion seam: convert an aware instant to the system-local zone.

    ``astimezone`` with no argument asks the platform for the local offset
    applicable to that specific instant, so it is portable and honours daylight
    transitions. Tests may replace this function to control the boundary.
    """
    return moment.astimezone()


def format_local_activity(value) -> str:
    """Display a stored activity instant in system-local time.

    Retained as the watch-specific wrapper for compatibility; the shared
    formatter in reporting owns conversion, fallbacks and the display format.
    """
    return format_local_timestamp(value, localize=_localize)


def status_table(store: StateStore, run_id: str) -> Table:
    try:
        state = store.load(run_id)
    except FileNotFoundError:
        table = Table(title=f"{run_id}: waiting for status snapshot")
        table.add_column("Status")
        table.add_row("Run state has not been published yet")
        return table
    table = Table(title=f"{state.slug}: {state.phase} / {state.stage} / cycle {state.round}")
    for title in ("Agent", "Status", "Attempt", "Provider", "Model", "Elapsed",
                  "Last activity (local)", "Checks", "Output"):
        table.add_column(title)
    for name, data in state.workers.items():
        tail = ""
        if data.get("log"):
            path = Path(data["log"])
            if path.exists():
                try:
                    with path.open("rb") as stream:
                        stream.seek(max(0, path.stat().st_size - 2048))
                        tail = stream.read().decode("utf-8", errors="replace").splitlines()[-2:]
                    tail = "\n".join(tail)
                except OSError:
                    tail = "Log temporarily unavailable"
        elapsed = data.get("duration_seconds", "")
        if data.get("started_at") and data.get("status") == "running":
            try:
                elapsed = round((datetime.now(UTC) - datetime.fromisoformat(data["started_at"])).total_seconds(), 1)
            except ValueError:
                elapsed = ""
        provider, model = attempt_identity(data, data.get("attempt_id"))
        values = [name, str(data.get("status", "pending")), str(data.get("attempt", 0)),
                  provider, model, str(elapsed), format_local_activity(data.get("activity")),
                  str(data.get("checks", "")), tail]
        # Log text is untrusted terminal input: strip ANSI/OSC/control sequences and
        # use literal Text so agent output cannot inject Rich markup.
        def safe(value):
            value = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", value)
            value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
            return Text("".join(c for c in value if c in "\n\t" or ord(c) >= 32 and ord(c) != 127))
        table.add_row(*(safe(value) for value in values))
    return table


def watch(store: StateStore, run_id: str, once: bool = False, interval: float = 1.0) -> None:
    console = Console()
    if once:
        console.print(status_table(store, run_id))
        return
    try:
        with Live(status_table(store, run_id), console=console, refresh_per_second=2) as live:
            while True:
                try:
                    live.update(status_table(store, run_id))
                except (OSError, ValueError):
                    pass  # A writer may be replacing a snapshot or recovering it.
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
