"""Small native-platform launch helpers; no shell interpolation of agent inputs."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class LaunchError(RuntimeError):
    pass


@dataclass(frozen=True)
class HelperResult:
    success: bool
    message: str


def resolve_executable(name: str, windows: bool | None = None) -> Path:
    windows = os.name == "nt" if windows is None else windows
    candidate = Path(name)
    if candidate.is_file():
        return candidate.resolve()
    found = shutil.which(name)
    if not found and windows:
        for suffix in (".exe", ".cmd", ".ps1"):
            found = shutil.which(name + suffix)
            if found:
                break
    if not found:
        raise LaunchError(f"Executable {name!r} was not found on PATH")
    resolved = Path(found).resolve()
    if windows and resolved.suffix.lower() in {".cmd", ".bat"}:
        powershell_shim = resolved.with_suffix(".ps1")
        if powershell_shim.is_file():
            return powershell_shim
    return resolved


def powershell_literal(value: str) -> str:
    if "\x00" in value:
        raise LaunchError("NUL is not supported in process arguments")
    return "'" + value.replace("'", "''") + "'"


def executable_argv(argv: list[str], windows: bool | None = None) -> list[str]:
    if not argv:
        raise LaunchError("Empty command")
    windows = os.name == "nt" if windows is None else windows
    resolved = resolve_executable(argv[0], windows=windows)
    if not windows or resolved.suffix.lower() not in {".ps1", ".cmd", ".bat"}:
        return [str(resolved), *map(str, argv[1:])]
    # Batch shims introduce a second cmd.exe parsing layer after PowerShell.
    if resolved.suffix.lower() in {".cmd", ".bat"} and any(
        any(c in str(arg) for c in '\r\n%!^&|<>"') for arg in argv[1:]
    ):
        raise LaunchError(
            "Batch shim arguments contain unsupported cmd metacharacters; use an executable or .ps1 shim"
        )
    script = "& " + " ".join(powershell_literal(str(arg)) for arg in [resolved, *argv[1:]])
    script += "; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }; if (-not $?) { exit 1 }"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        str(resolve_executable("pwsh")),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded,
    ]


def shell_argv(command: str, policy: str) -> list[str]:
    if policy == "bash":
        return [str(resolve_executable("bash")), "-lc", command]
    if policy == "pwsh":
        script = (
            command
            + "\nif ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }; if (-not $?) { exit 1 }"
        )
        return [
            str(resolve_executable("pwsh")),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ]
    raise LaunchError(f"Unknown shell policy: {policy!r}")


def launch_viewer(slug: str, executable: str = "orch", data_dir: Path | None = None) -> HelperResult:
    command = [executable]
    if data_dir is not None:
        command += ["--data-dir", str(data_dir)]
    command += ["watch", slug]
    import shlex
    display = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
    try:
        if os.name == "nt" and shutil.which("wt"):
            subprocess.Popen(
                [
                    "wt",
                    "new-tab",
                    "pwsh",
                    "-NoLogo",
                    "-NoExit",
                    "-Command",
                    "& " + " ".join(powershell_literal(x) for x in command),
                ]
            )
        elif shutil.which("tmux") and os.environ.get("TMUX"):
            # tmux executes a shell command; POSIX quote each argument explicitly.
            import shlex

            subprocess.Popen(["tmux", "split-window", "-h", shlex.join(command)])
        else:
            return HelperResult(False, f"Open another terminal and run: {display}")
        return HelperResult(True, "Viewer launched")
    except OSError as exc:
        return HelperResult(False, f"Viewer unavailable: {exc}; run {display}")


def notify(title: str, message: str, webhook_url: str | None = None) -> HelperResult:
    """Send only an already-redacted summary. Never include webhook URLs in errors."""
    try:
        if webhook_url:
            if not webhook_url.startswith(("https://", "http://")):
                return HelperResult(False, "Webhook requires HTTP or HTTPS")
            payload = json.dumps({"title": title[:200], "message": message[:2000]}).encode()
            request = urllib.request.Request(
                webhook_url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=5):
                pass
            return HelperResult(True, "Webhook notification sent")
        if os.name != "nt" and shutil.which("notify-send"):
            result = subprocess.run(
                ["notify-send", title[:200], message[:2000]],
                capture_output=True,
                timeout=5,
                check=False,
            )
            return HelperResult(result.returncode == 0, "Desktop notification attempted")
        return HelperResult(False, "Desktop notifications unavailable; report is retained on disk")
    except OSError, ValueError, subprocess.SubprocessError, urllib.error.URLError:
        return HelperResult(False, "Notification failed; report is retained on disk")
