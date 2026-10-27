"""Owned asynchronous subprocesses with redacted logs and process-tree cancellation."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil

from orch.platform_support import executable_argv, shell_argv
from orch.store import timestamp


class Redactor:
    def __init__(self, secrets=()):
        self.secrets = sorted({str(s) for s in secrets if s}, key=len, reverse=True)

    def __call__(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(
            r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s\"']+", r"\1[REDACTED]", value
        )
        value = re.sub(
            r"(?i)([\"']?(?:api[_-]?key|password|access[_-]?token|secret)[\"']?\s*[=:]\s*[\"']?)[^\s\"',}]+",
            r"\1[REDACTED]",
            value,
        )
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)
        return value


@dataclass(frozen=True)
class ProcessHandle:
    pid: int
    create_time: float


@dataclass
class ProcessResult:
    exit_code: int
    duration_seconds: float
    stdout_log: Path
    stderr_log: Path
    reason: str | None = None
    handle: ProcessHandle | None = None


async def emit(callback, value: dict) -> None:
    if callback is not None:
        result = callback(value)
        if inspect.isawaitable(result):
            await result


class ProcessRunner:
    def __init__(self, secrets=()):
        self.redact = Redactor(secrets)
        self.handles: dict[int, ProcessHandle] = {}
        self.children: dict[int, dict[int, ProcessHandle]] = {}

    async def cancel(self, handle: ProcessHandle, grace_seconds: float = 1.0) -> None:
        processes = []
        try:
            parent = psutil.Process(handle.pid)
            if abs(parent.create_time() - handle.create_time) > 0.01:
                return
            processes = [*parent.children(recursive=True), parent]
        except psutil.NoSuchProcess:
            pass
        for child in self.children.get(handle.pid, {}).values():
            try:
                process = psutil.Process(child.pid)
                if abs(process.create_time() - child.create_time) < 0.01:
                    processes.append(process)
            except psutil.NoSuchProcess:
                pass
        # The group is owned for the lifetime of this live invocation, including when
        # its leader exits before children close stdout. Recovered handles require a
        # verified parent/recorded descendant; never signal an unverified stale group.
        # A remembered handle alone is not enough to authorize killpg: after a
        # crash, its PID/process-group ID may have been reused. Only signal the
        # original session group while the original leader identity still
        # matches and is still its group leader. Descendants are individually
        # terminated above if the leader has already exited.
        def group_is_owned() -> bool:
            if os.name == "nt":
                return False
            try:
                current = psutil.Process(handle.pid)
                if abs(current.create_time() - handle.create_time) <= 0.01:
                    return os.getpgid(handle.pid) == handle.pid
                # A different process now owns the PID; never signal its group.
                return False
            except psutil.NoSuchProcess:
                # During this live invocation, the leader can exit before a
                # descendant closes inherited pipes. The active handle plus a
                # surviving member in its isolated session establishes that
                # this original process group still exists. Recovered handles
                # without a verified leader are deliberately not sufficient.
                if self.handles.get(handle.pid) != handle:
                    return False
                try:
                    for process in psutil.process_iter():
                        if process.pid == handle.pid:
                            continue
                        if (
                            os.getpgid(process.pid) == handle.pid
                            and os.getsid(process.pid) == handle.pid
                        ):
                            return True
                except (psutil.Error, ProcessLookupError, PermissionError):
                    return False
                return False
            except (ProcessLookupError, PermissionError):
                return False

        if group_is_owned():
            try:
                os.killpg(handle.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process in processes:
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = await asyncio.to_thread(psutil.wait_procs, processes, timeout=grace_seconds)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        if group_is_owned():
            try:
                os.killpg(handle.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def cancel_all(self) -> None:
        await asyncio.gather(*(self.cancel(h) for h in list(self.handles.values())))

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        timeout: float = 300,
        log_paths: tuple[Path, Path] | None = None,
        shell_policy: str | None = None,
        on_event: Callable | None = None,
        stdin: str | bytes | None = None,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        cwd = Path(cwd).resolve()
        if timeout <= 0:
            raise ValueError("Process timeout must be positive")
        if log_paths is None:
            raise ValueError("Explicit log_paths outside project worktrees are required")
        stdout_path, stderr_path = map(Path, log_paths)
        for path in (stdout_path, stderr_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        command = shell_argv(argv[0], shell_policy) if shell_policy else executable_argv(argv)
        kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000200}
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        try:
            created = psutil.Process(process.pid).create_time()
        except psutil.NoSuchProcess:
            # A very short process may already be reaped. Never use an invented
            # creation time to authorize a later signal.
            created = 0.0
        handle = ProcessHandle(process.pid, created)
        self.handles[handle.pid] = handle
        tasks = []
        monitor = None
        reason = None
        try:
            await emit(
                on_event,
                {
                    "kind": "process_started",
                    "time": timestamp(),
                    "pid": handle.pid,
                    "create_time": handle.create_time,
                },
            )

            async def track_children():
                known = self.children.setdefault(handle.pid, {})
                while True:
                    try:
                        parent = psutil.Process(handle.pid)
                        if abs(parent.create_time() - handle.create_time) > 0.01:
                            return
                        changed = False
                        for child in parent.children(recursive=True):
                            try:
                                identity = ProcessHandle(child.pid, child.create_time())
                            except psutil.NoSuchProcess:
                                continue
                            if known.get(child.pid) != identity:
                                known[child.pid] = identity
                                changed = True
                        if changed:
                            await emit(
                                on_event,
                                {
                                    "kind": "process_children",
                                    "time": timestamp(),
                                    "pid": handle.pid,
                                    "children": [
                                        {"pid": c.pid, "create_time": c.create_time}
                                        for c in known.values()
                                    ],
                                },
                            )
                    except psutil.NoSuchProcess:
                        return
                    await asyncio.sleep(0.05)

            monitor = asyncio.create_task(track_children())

            async def pump(stream, path, name):
                # Buffer a complete line before redaction so a secret split between OS reads
                # is never partially persisted. Boundless agent lines are not normal CLI output.
                pending = b""
                with path.open("w", encoding="utf-8", newline="\n") as output:
                    while chunk := await stream.read(4096):
                        pending += chunk
                        if len(pending) > 1024 * 1024:
                            raise ValueError(
                                "Agent output line exceeded 1 MiB; refusing unbounded buffering"
                            )
                        lines = pending.split(b"\n")
                        pending = lines.pop()
                        for line in lines:
                            clean = self.redact(line.decode("utf-8", errors="replace"))
                            output.write(clean + "\n")
                            output.flush()
                            await emit(
                                on_event,
                                {
                                    "kind": "output",
                                    "time": timestamp(),
                                    "stream": name,
                                    "text": clean,
                                },
                            )
                    if pending:
                        clean = self.redact(pending.decode("utf-8", errors="replace"))
                        output.write(clean)
                        output.flush()
                        await emit(
                            on_event,
                            {"kind": "output", "time": timestamp(), "stream": name, "text": clean},
                        )

            tasks = [
                asyncio.create_task(pump(process.stdout, stdout_path, "stdout")),
                asyncio.create_task(pump(process.stderr, stderr_path, "stderr")),
            ]
            if stdin is not None:
                process.stdin.write(stdin.encode() if isinstance(stdin, str) else stdin)
                await process.stdin.drain()
                process.stdin.close()
            try:
                async with asyncio.timeout(timeout):
                    await process.wait()
                    await asyncio.shield(asyncio.gather(*tasks))
            except TimeoutError:
                reason = "timeout"
                await self.cancel(handle)
                await process.wait()
                await asyncio.gather(*tasks)
            await emit(
                on_event,
                {
                    "kind": "process_completed",
                    "time": timestamp(),
                    "pid": handle.pid,
                    "create_time": handle.create_time,
                    "exit_code": process.returncode,
                    "reason": reason,
                },
            )
            return ProcessResult(
                process.returncode,
                time.monotonic() - started,
                stdout_path,
                stderr_path,
                reason,
                handle,
            )
        except BaseException:
            await self.cancel(handle)
            await process.wait()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            if monitor:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            self.handles.pop(handle.pid, None)
            self.children.pop(handle.pid, None)


def reconcile_events(events: list[dict]) -> list[dict]:
    """Inspect durable process events without signalling or relaunching anything.

    Workflow must scope events to a run and record dispatch identity separately.
    Unknown ownership is an escalation condition, not authorization to kill a PID.
    """
    identities = {}
    for event in events:
        kind = event.get("kind")
        if kind == "process_started":
            identities[event["pid"]] = {
                "pid": event["pid"],
                "create_time": event["create_time"],
                "children": [],
            }
        elif kind == "process_children" and event.get("pid") in identities:
            identities[event["pid"]]["children"] = event.get("children", [])
        elif kind == "process_completed":
            identities.pop(event.get("pid"), None)
    result = []
    for identity in identities.values():
        live = []
        reused = False
        for candidate in [identity, *identity["children"]]:
            try:
                process = psutil.Process(candidate["pid"])
                if abs(process.create_time() - candidate["create_time"]) < 0.01:
                    if process.status() != psutil.STATUS_ZOMBIE:
                        live.append(
                            {"pid": candidate["pid"], "create_time": candidate["create_time"]}
                        )
                elif candidate is identity:
                    reused = True
            except psutil.NoSuchProcess:
                pass
        result.append(
            {
                **identity,
                "status": "alive" if live else "reused" if reused else "exited",
                "live": live,
            }
        )
    return result
