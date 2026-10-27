import asyncio
import os
import sys

import psutil
import pytest

from orch.processes import ProcessRunner, Redactor


def test_streams_redacted_before_disk_and_callback(tmp_path):
    script = "import sys,time; sys.stdout.write('abcSECRET');sys.stdout.flush();time.sleep(.05);print('VALUExyz');print('password=hidden',file=sys.stderr)"
    events = []
    result = asyncio.run(
        ProcessRunner(secrets=["SECRETVALUE"]).run(
            [sys.executable, "-c", script],
            tmp_path,
            log_paths=(tmp_path / "out", tmp_path / "err"),
            on_event=events.append,
        )
    )
    assert result.exit_code == 0
    assert result.stdout_log.read_text() == "abc[REDACTED]xyz\n"
    assert "hidden" not in result.stderr_log.read_text()
    assert "SECRETVALUE" not in str(events)
    assert events[0]["kind"] == "process_started"


def test_timeout_terminates_tree(tmp_path):
    script = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); print(p.pid,flush=True);time.sleep(60)"
    result = asyncio.run(
        ProcessRunner().run(
            [sys.executable, "-c", script],
            tmp_path,
            timeout=0.5,
            log_paths=(tmp_path / "out", tmp_path / "err"),
        )
    )
    assert result.reason == "timeout"
    child = int(result.stdout_log.read_text().strip())
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE


def test_task_cancel_terminates_tree(tmp_path):
    async def scenario():
        runner = ProcessRunner()
        event = asyncio.Event()

        def callback(value):
            if value["kind"] == "output":
                event.set()

        task = asyncio.create_task(
            runner.run(
                [sys.executable, "-c", "import time;print('ready',flush=True);time.sleep(60)"],
                tmp_path,
                log_paths=(tmp_path / "out", tmp_path / "err"),
                on_event=callback,
            )
        )
        await asyncio.wait_for(event.wait(), 5)
        pid = next(iter(runner.handles))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not psutil.pid_exists(pid)
        assert not runner.handles

    asyncio.run(scenario())


def test_process_exit_and_shell_policy(tmp_path):
    result = asyncio.run(
        ProcessRunner().run(
            [sys.executable, "-c", "raise SystemExit(7)"],
            tmp_path,
            log_paths=(tmp_path / "out", tmp_path / "err"),
        )
    )
    assert result.exit_code == 7
    if os.name != "nt":
        result = asyncio.run(
            ProcessRunner().run(
                ["exit 9"],
                tmp_path,
                shell_policy="bash",
                log_paths=(tmp_path / "out", tmp_path / "err"),
            )
        )
        assert result.exit_code == 9


def test_redactor_patterns():
    redactor = Redactor()
    assert "abc" not in redactor("Authorization: Bearer abc")
    assert "sk-" not in redactor("sk-abcdefghijklmnop")
    assert '"api_key":"[REDACTED]"' in redactor('{"api_key":"hidden-value"}')


def test_stale_handle_does_not_authorize_process_group_signal(monkeypatch):
    from orch.processes import ProcessHandle

    runner = ProcessRunner()
    handle = ProcessHandle(987654321, 123.0)
    runner.handles[handle.pid] = handle
    signals = []

    def missing_process(_pid):
        raise psutil.NoSuchProcess(987654321)

    monkeypatch.setattr("orch.processes.psutil.Process", missing_process)
    monkeypatch.setattr("orch.processes.os.killpg", lambda *args: signals.append(args))
    asyncio.run(runner.cancel(handle))
    assert signals == []


def test_callback_failure_cleans_process(tmp_path):
    async def scenario():
        runner = ProcessRunner()
        seen = []

        def callback(event):
            if event["kind"] == "process_started":
                seen.append(event["pid"])
                raise RuntimeError("status writer failed")

        with pytest.raises(RuntimeError, match="status writer failed"):
            await runner.run(
                [sys.executable, "-c", "import time;time.sleep(60)"],
                tmp_path,
                log_paths=(tmp_path / "out", tmp_path / "err"),
                on_event=callback,
            )
        assert not psutil.pid_exists(seen[0])
        assert not runner.handles

    asyncio.run(scenario())


def test_timeout_parent_exited_child_keeps_pipe(tmp_path):
    script = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);print(p.pid,flush=True)"
    result = asyncio.run(
        ProcessRunner().run(
            [sys.executable, "-c", script],
            tmp_path,
            timeout=0.3,
            log_paths=(tmp_path / "out", tmp_path / "err"),
        )
    )
    assert result.reason == "timeout"
    child = int(result.stdout_log.read_text().strip())
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE


def test_process_reconciliation_never_trusts_reused_pid():
    from orch.processes import reconcile_events

    current = psutil.Process()
    events = [
        {"kind": "process_started", "pid": current.pid, "create_time": current.create_time() - 1}
    ]
    assert reconcile_events(events)[0]["status"] == "reused"
    events[0]["create_time"] = current.create_time()
    assert reconcile_events(events)[0]["status"] == "alive"
    events.append({"kind": "process_completed", "pid": current.pid})
    assert reconcile_events(events) == []
