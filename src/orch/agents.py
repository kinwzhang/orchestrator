"""Version-probed agent CLI adapters. All substantive instructions remain in Markdown."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import tempfile
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


def _capture(argv: list[str], *, cwd: Path | None = None, env: dict | None = None) -> str:
    result = subprocess.run(
        executable_argv(argv),
        cwd=cwd,
        env=env,
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


_DOCS_REVIEW_DISABLED_TOOLS = (
    "bash",
    "glob",
    "grep",
    "lsp",
    "task",
    "skill",
    "webfetch",
    "websearch",
)
_DOCS_REVIEW_DENIED_ACTIONS = (
    "shell",
    "glob",
    "grep",
    "lsp",
    "subagent",
    "skill",
    "webfetch",
    "websearch",
)


def docs_access_profile(request: AgentRequest, profile_cwd: Path | None = None) -> dict:
    """Build a deny-by-default OpenCode profile for a curated docs review.

    Only the Markdown instruction and supplied curated Markdown files may be read;
    only the requested verdict may be edited. The cwd must be outside every Git
    checkout so a project config cannot supersede this generated profile.
    """
    if request.role != "docs_reviewer":
        raise LaunchError("A docs access profile is only valid for the docs_reviewer role")
    cwd = Path(request.cwd).resolve()
    if not cwd.is_dir():
        raise LaunchError("Docs reviewer working directory must be an existing curated directory")
    if any((parent / ".git").exists() for parent in (cwd, *cwd.parents)):
        raise LaunchError(
            "Docs reviewer access policy requires a curated working directory outside Git checkouts"
        )
    if any(
        (parent / name).exists()
        for parent in (cwd, *cwd.parents)
        for name in ("opencode.json", "opencode.jsonc")
    ) or (cwd / ".opencode").exists():
        raise LaunchError(
            "Docs reviewer access policy refuses OpenCode project configuration in its directory ancestry"
        )
    profile_cwd = Path(profile_cwd or cwd).resolve()
    if not profile_cwd.is_dir() or any(
        (parent / ".git").exists() for parent in (profile_cwd, *profile_cwd.parents)
    ):
        raise LaunchError("Docs reviewer policy directory must be outside Git checkouts")

    def markdown_path(path: Path, *, must_exist: bool) -> Path:
        path = Path(path)
        if path.suffix.lower() != ".md":
            raise LaunchError("Docs reviewer access policy allows Markdown files only")
        try:
            resolved = path.resolve(strict=must_exist)
        except (OSError, RuntimeError) as exc:
            raise LaunchError("Docs reviewer access policy input is unavailable") from exc
        if any(char in str(resolved) for char in "*?"):
            raise LaunchError("Docs reviewer access policy cannot safely match wildcard path characters")
        if must_exist and not resolved.is_file():
            raise LaunchError("Docs reviewer access policy input must be a regular Markdown file")
        if any((parent / ".git").exists() for parent in (resolved.parent, *resolved.parents)):
            raise LaunchError("Docs reviewer access policy refuses files inside a Git checkout")
        return resolved

    curated_inputs = []
    for candidate in (request.instruction_file, *request.input_files):
        path = markdown_path(Path(candidate), must_exist=True)
        if candidate != request.instruction_file and not path.is_relative_to(cwd):
            raise LaunchError(
                "Docs reviewer inputs must be copied into its curated working directory"
            )
        curated_inputs.append(path)

    output_path = Path(request.output_file)
    if output_path.is_symlink():
        raise LaunchError("Docs reviewer verdict path cannot be a symbolic link")
    output = markdown_path(output_path, must_exist=False)
    read_paths = sorted({str(path) for path in (*curated_inputs, output)})
    external_directories = sorted(
        {
            str(path.parent / "**")
            for path in (*curated_inputs, output)
            if not path.is_relative_to(profile_cwd)
        }
    )
    permission = {
        "*": "deny",
        "read": {"*": "deny", **{path: "allow" for path in read_paths}},
        "edit": {"*": "deny", str(output): "allow"},
        "external_directory": {
            "*": "deny",
            **{pattern: "allow" for pattern in external_directories},
        },
    }
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": permission,
        "tools": {name: False for name in _DOCS_REVIEW_DISABLED_TOOLS},
        "plugin": [],
        # MCP tools are still denied by the wildcard permission, even when
        # user configuration defines servers unknown to this adapter.
        "mcp": {},
    }


def _verify_docs_access_profile(
    executable: str, config_path: Path, cwd: Path, env: dict
) -> None:
    """Ask the installed CLI to parse the profile and verify its resolved rules."""
    try:
        sources = json.loads(
            _capture([executable, "debug", "config"], cwd=cwd, env=env)
        )
    except (LaunchError, json.JSONDecodeError, TypeError) as exc:
        raise LaunchError(
            "OpenCode docs-role access policy could not be validated; refusing docs review"
        ) from exc
    if not isinstance(sources, list):
        raise LaunchError("OpenCode docs-role access policy returned an invalid config report")
    normalized_config = str(config_path.resolve())
    documents = [source for source in sources if source.get("type") == "document"]
    profile_index = next(
        (
            index
            for index, source in enumerate(documents)
            if str(Path(source.get("path", "")).resolve()) == normalized_config
        ),
        None,
    )
    if profile_index is None:
        raise LaunchError("OpenCode did not load the docs-role access policy; refusing docs review")
    profile = documents[profile_index].get("info", {})
    rules = profile.get("permissions")
    if not isinstance(rules, list):
        raise LaunchError("OpenCode did not expose resolved docs-role permissions; refusing review")

    def find_rule(action: str, resource: str, effect: str, after: int = -1) -> int | None:
        return next(
            (
                index
                for index, rule in enumerate(rules)
                if index > after
                and rule.get("action") == action
                and rule.get("resource") == resource
                and rule.get("effect") == effect
            ),
            None,
        )

    deny_all = find_rule("*", "*", "deny")
    read_deny = find_rule("read", "*", "deny", deny_all if deny_all is not None else -1)
    edit_deny = find_rule("edit", "*", "deny", deny_all if deny_all is not None else -1)
    external_deny = find_rule(
        "external_directory", "*", "deny", deny_all if deny_all is not None else -1
    )
    if None in (deny_all, read_deny, edit_deny, external_deny):
        raise LaunchError("OpenCode omitted a required docs-role deny rule; refusing docs review")
    if any(find_rule(action, "*", "deny") is None for action in _DOCS_REVIEW_DENIED_ACTIONS):
        raise LaunchError("OpenCode omitted a source-access tool denial; refusing docs review")
    for later in documents[profile_index + 1 :]:
        later_info = later.get("info", {})
        if later_info.get("plugins") or later_info.get("mcp"):
            raise LaunchError(
                "A later OpenCode config enables plugins or MCP tools; refusing docs review"
            )
        for rule in later_info.get("permissions", []):
            if rule.get("effect") == "allow":
                raise LaunchError(
                    "A later OpenCode config adds an allow rule after docs-role denies; refusing review"
                )

    # `read` and `edit` grants must be exact file paths and occur after their
    # corresponding deny-all rules. Verify the CLI did not broaden a grant.
    config = json.loads(config_path.read_text(encoding="utf-8"))
    permissions = config["permission"]
    for path, effect in permissions["read"].items():
        expected = "deny" if path == "*" else "allow"
        if effect != expected:
            raise LaunchError("Invalid docs-role read policy; refusing docs review")
        if path != "*" and find_rule("read", path, "allow", read_deny) is None:
            raise LaunchError("OpenCode did not preserve a curated Markdown read grant")
    output_path = next(path for path, effect in permissions["edit"].items() if path != "*")
    if find_rule("edit", output_path, "allow", edit_deny) is None:
        raise LaunchError("OpenCode did not preserve the verdict-only write grant")
    for path, effect in permissions["external_directory"].items():
        expected = "deny" if path == "*" else "allow"
        if effect != expected:
            raise LaunchError("Invalid docs-role external-path policy; refusing docs review")
        if path != "*" and find_rule(
            "external_directory", path, "allow", external_deny
        ) is None:
            raise LaunchError("OpenCode did not preserve an exact external Markdown path grant")
    if profile.get("plugins") != []:
        raise LaunchError("OpenCode docs-role profile did not disable plugins; refusing review")


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

    @contextlib.contextmanager
    def request_environment(self, request: AgentRequest):
        yield Path(request.cwd), None

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

        with self.request_environment(request) as (cwd, env):
            capabilities = await asyncio.to_thread(self.capabilities)
            command = self.command(request)
            result = await self.runner.run(
                command,
                cwd,
                timeout=request.timeout_seconds,
                log_paths=(request.log_dir / "stdout.log", request.log_dir / "stderr.log"),
                on_event=event,
                env=env,
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

    @contextlib.contextmanager
    def request_environment(self, request: AgentRequest):
        if request.role != "docs_reviewer":
            yield Path(request.cwd), None
            return
        try:
            # OpenCode v2.1.4 only loaded OPENCODE_CONFIG when it was rooted in
            # the selected project directory during local CLI probes. Run from a
            # private child of the curated workspace, never from a source tree.
            with tempfile.TemporaryDirectory(
                prefix=".orch-opencode-docs-", dir=request.cwd
            ) as temporary:
                root = Path(temporary)
                config_path = root / "opencode.json"
                config_path.write_text(
                    json.dumps(docs_access_profile(request, root), ensure_ascii=False),
                    encoding="utf-8",
                )
                config_dir = root / "config-dir"
                config_dir.mkdir()
                env = os.environ.copy()
                env["OPENCODE_CONFIG"] = str(config_path)
                env["OPENCODE_CONFIG_DIR"] = str(config_dir)
                # Inline config has higher precedence than the generated policy.
                env.pop("OPENCODE_CONFIG_CONTENT", None)
                _verify_docs_access_profile(self.executable, config_path, root, env)
                yield root, env
        except LaunchError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise LaunchError(
                "OpenCode docs-role access policy setup failed; refusing docs review"
            ) from exc

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
