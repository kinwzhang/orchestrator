"""Version-probed agent CLI adapters. All substantive instructions remain in Markdown."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from orch.contracts import ModelSpec
from orch.platform_support import LaunchError, executable_argv
from orch.processes import ProcessRunner, emit
from orch.store import atomic_write


@dataclass
class AgentRequest:
    role: str
    logical_id: str
    attempt_id: str
    instruction_file: Path
    output_file: Path
    cwd: Path
    model: str | ModelSpec
    timeout_seconds: float = 2700
    input_files: tuple[Path, ...] = ()
    log_dir: Path | None = None
    writable_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class AgentCapabilities:
    version: str
    flags: frozenset[str]
    event_format: str = "jsonl"
    isolation_mode: str = "private-process"
    cancellation: str = "owned-process-tree"
    attachment: bool = False


@dataclass
class AgentResult:
    exit_code: int
    duration_seconds: float
    stdout_log: Path
    stderr_log: Path
    reason: str | None = None
    session_id: str | None = None
    usage: dict = field(default_factory=dict)
    adapter_version: str = "unknown"


def _capture(argv: list[str]) -> str:
    result = subprocess.run(
        executable_argv(argv),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise LaunchError("Agent capability probe failed; verify CLI installation")
    return result.stdout + result.stderr


class AgentAdapter:
    tool = ""

    def __init__(
        self, runner: ProcessRunner | None = None, executable: str | None = None, secrets=()
    ):
        self.executable = executable or self.tool
        self.runner = runner or ProcessRunner(secrets)
        self._capabilities: AgentCapabilities | None = None

    def capabilities(self) -> AgentCapabilities:
        if self._capabilities is None:
            version = _capture([self.executable, "--version"]).strip()
            help_text = _capture(
                [self.executable, "exec" if self.tool == "codex" else "run", "--help"]
            )
            flags = frozenset(re.findall(r"--[a-z][a-z-]*", help_text))
            required = (
                {"--json", "--model", "--sandbox", "--output-last-message"}
                if self.tool == "codex"
                else {"--format", "--model", "--standalone", "--auto"}
            )
            missing = required - flags
            if missing:
                raise LaunchError(f"{version}: missing required flags {', '.join(sorted(missing))}")
            self._capabilities = AgentCapabilities(version, flags)
        return self._capabilities

    def wrapper(self, request: AgentRequest) -> str:
        lines = [
            "Read and follow this Markdown instruction file:",
            str(request.instruction_file.resolve()),
            "Write the requested Markdown result to:",
            str(request.output_file.resolve()),
        ]
        if request.input_files:
            lines.extend(
                [
                    "Read these additional Markdown inputs:",
                    *(str(Path(p).resolve()) for p in request.input_files),
                ]
            )
        lines.append(
            "Inspect existing work and partial handovers before acting. Continue retained work."
        )
        if request.role == "docs_reviewer":
            lines.append(
                "Read only the supplied documentation and handovers. Judge relevance only. Do not read code, source diffs, or code-bearing logs."
            )
        return "\n".join(lines)

    @staticmethod
    def model(request: AgentRequest) -> ModelSpec:
        return (
            request.model
            if isinstance(request.model, ModelSpec)
            else ModelSpec(model=request.model)
        )

    def command(self, request: AgentRequest) -> list[str]:
        raise NotImplementedError

    async def run(self, request: AgentRequest, on_event: Callable | None = None) -> AgentResult:
        for path in (request.instruction_file, *request.input_files):
            if Path(path).suffix.lower() != ".md" or not Path(path).is_file():
                raise LaunchError("Agent inputs must be existing Markdown files")
        if request.output_file.suffix.lower() != ".md":
            raise LaunchError("Agent result must be a Markdown file")
        if request.log_dir is None:
            raise LaunchError("Agent log_dir must be provided outside its worktree")
        request.output_file.parent.mkdir(parents=True, exist_ok=True)
        request.log_dir.mkdir(parents=True, exist_ok=True)
        capabilities = await asyncio.to_thread(self.capabilities)
        command = self.command(request)
        session_id = None
        usage = {}

        async def event(value):
            nonlocal session_id, usage
            if value.get("kind") == "output" and value.get("stream") == "stdout":
                try:
                    item = json.loads(value["text"])
                except json.JSONDecodeError, TypeError:
                    item = {}
                if isinstance(item, dict):
                    identifier = (
                        item.get("thread_id") or item.get("sessionID") or item.get("session_id")
                    )
                    if isinstance(identifier, str):
                        session_id = identifier
                    candidate = item.get("usage")
                    if candidate is None and isinstance(item.get("part"), dict):
                        candidate = item["part"].get("tokens")
                    if isinstance(candidate, dict):
                        usage = candidate
            await emit(on_event, value)

        result = await self.runner.run(
            command,
            request.cwd,
            timeout=request.timeout_seconds,
            log_paths=(request.log_dir / "stdout.log", request.log_dir / "stderr.log"),
            on_event=event,
        )
        if request.output_file.exists():
            content = request.output_file.read_text(encoding="utf-8", errors="replace")
            sanitized = self.runner.redact(content)
            if sanitized != content:
                atomic_write(request.output_file, sanitized)
                raise LaunchError(
                    "Agent result contained recognizable sensitive material; sanitized output requires repair"
                )
        return AgentResult(
            result.exit_code,
            result.duration_seconds,
            result.stdout_log,
            result.stderr_log,
            result.reason,
            session_id,
            usage,
            capabilities.version,
        )


class CodexAdapter(AgentAdapter):
    tool = "codex"

    def command(self, request: AgentRequest) -> list[str]:
        capabilities = self.capabilities()
        model = self.model(request)
        result = [
            self.executable,
            "exec",
            "--json",
            "--model",
            model.model,
            "--sandbox",
            "workspace-write",
            "--output-last-message",
            str(request.output_file.resolve()),
            "--cd",
            str(request.cwd.resolve()),
        ]
        if model.reasoning:
            if "--config" not in capabilities.flags:
                raise LaunchError("Installed Codex does not support reasoning configuration")
            result += ["--config", "model_reasoning_effort=" + json.dumps(model.reasoning)]
        for root in request.writable_roots:
            if "--add-dir" not in capabilities.flags:
                raise LaunchError("Installed Codex cannot add the output writable directory")
            result += ["--add-dir", str(root.resolve())]
        result.append(self.wrapper(request))
        return result

    def interactive_argv(self, request: AgentRequest) -> list[str]:
        self.capabilities()
        model = self.model(request)
        result = [
            self.executable,
            "--model",
            model.model,
            "--cd",
            str(request.cwd.resolve()),
            "--sandbox",
            "workspace-write",
        ]
        if model.reasoning:
            result += ["--config", "model_reasoning_effort=" + json.dumps(model.reasoning)]
        for root in request.writable_roots:
            result += ["--add-dir", str(root.resolve())]
        return executable_argv([*result, self.wrapper(request)])


class OpenCodeAdapter(AgentAdapter):
    tool = "opencode"

    def command(self, request: AgentRequest) -> list[str]:
        capabilities = self.capabilities()
        model = self.model(request)
        model_id = model.model
        if model.reasoning:
            if "--variant" in capabilities.flags:
                variant = ["--variant", model.reasoning]
            elif "#" not in model_id and capabilities.version.startswith("opencode v2."):
                model_id += "#" + model.reasoning
                variant = []
            else:
                raise LaunchError("Installed OpenCode has no supported reasoning variant mechanism")
        else:
            variant = []
        result = [
            self.executable,
            "run",
            "--standalone",
            "--format",
            "json",
            "--model",
            model_id,
            *variant,
        ]
        # --auto approves permissions not explicitly denied in the user's agent policy.
        # Avoid bypassing that policy; absent capability must fail rather than hang.
        if "--auto" not in capabilities.flags:
            raise LaunchError(
                "Installed OpenCode cannot run unattended under its configured permission policy"
            )
        return [*result, "--auto", self.wrapper(request)]
