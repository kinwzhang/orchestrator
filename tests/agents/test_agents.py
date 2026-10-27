import asyncio
import base64
import sys
import urllib.error
from pathlib import Path

import pytest

from orch.agents import AgentCapabilities, AgentRequest, CodexAdapter, OpenCodeAdapter
from orch.contracts import ModelSpec
from orch.platform_support import (
    LaunchError,
    executable_argv,
    launch_viewer,
    notify,
    powershell_literal,
    resolve_executable,
)


def request(tmp_path, **overrides):
    instruction = tmp_path / "instruction.md"
    instruction.write_text("# Task")
    values = {
        "role": "worker",
        "logical_id": "w01",
        "attempt_id": "a1",
        "instruction_file": instruction,
        "output_file": tmp_path / "handover.md",
        "cwd": tmp_path,
        "model": "provider/coder",
        "log_dir": tmp_path / "logs",
    }
    return AgentRequest(**(values | overrides))


def test_opencode_fresh_isolated_model(tmp_path):
    adapter = OpenCodeAdapter()
    adapter._capabilities = AgentCapabilities(
        "opencode v2.1.4", frozenset({"--format", "--model", "--standalone", "--auto"})
    )
    argv = adapter.command(
        request(tmp_path, model=ModelSpec(model="provider/coder", reasoning="high"))
    )
    assert "--standalone" in argv
    assert "provider/coder#high" in argv
    assert "--session" not in argv and "--continue" not in argv
    assert str((tmp_path / "instruction.md").resolve()) in argv[-1]


def test_missing_unattended_capability_fails(tmp_path):
    adapter = OpenCodeAdapter()
    adapter._capabilities = AgentCapabilities("unknown", frozenset())
    with pytest.raises(LaunchError, match="unattended"):
        adapter.command(request(tmp_path))


def test_opencode_preflight_requires_unattended_flag(monkeypatch):
    def probe(argv):
        if argv[1] == "--version":
            return "opencode v2.test"
        return "--format --model --standalone"

    monkeypatch.setattr("orch.agents._capture", probe)
    with pytest.raises(LaunchError, match="--auto"):
        OpenCodeAdapter().capabilities()


def test_codex_explicit_model_output_and_reasoning(tmp_path):
    adapter = CodexAdapter()
    adapter._capabilities = AgentCapabilities("codex test", frozenset({"--config", "--add-dir"}))
    argv = adapter.command(
        request(
            tmp_path,
            model=ModelSpec(model="test", reasoning="high"),
            writable_roots=(tmp_path / "external",),
        )
    )
    assert "--output-last-message" in argv
    assert 'model_reasoning_effort="high"' in argv
    assert "--add-dir" in argv


def test_adapter_stub_usage_and_file_validation(tmp_path):
    class Stub(OpenCodeAdapter):
        def capabilities(self):
            return AgentCapabilities("stub", frozenset())

        def command(self, request):
            return [
                sys.executable,
                "-c",
                'print(\'{"sessionID":"ses_1","usage":{"input_tokens":12}}\')',
            ]

    result = asyncio.run(Stub().run(request(tmp_path)))
    assert result.session_id == "ses_1"
    assert result.usage == {"input_tokens": 12}
    assert result.adapter_version == "stub"
    with pytest.raises(LaunchError, match="Markdown"):
        asyncio.run(Stub().run(request(tmp_path, output_file=tmp_path / "bad.json")))


def test_docs_wrapper_forbids_code(tmp_path):
    text = OpenCodeAdapter().wrapper(request(tmp_path, role="docs_reviewer"))
    assert "Judge relevance only" in text
    assert "Do not read code" in text


def test_powershell_shim_quotes_literals(tmp_path, monkeypatch):
    shim = tmp_path / "agent.ps1"
    shim.touch()
    monkeypatch.setattr(
        "orch.platform_support.resolve_executable",
        lambda name, windows=None: Path(name) if name != "pwsh" else Path("/pwsh"),
    )
    argv = executable_argv([str(shim), "a 'quoted' $(bad) value"], windows=True)
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert "'a ''quoted'' $(bad) value'" in script
    assert argv[:2] == ["/pwsh", "-NoLogo"]
    assert powershell_literal("a'b") == "'a''b'"


def test_windows_batch_shim_accepts_simple_spaced_arguments(tmp_path, monkeypatch):
    shim = tmp_path / "agent.cmd"
    shim.touch()
    monkeypatch.setattr(
        "orch.platform_support.resolve_executable",
        lambda name, windows=None: Path(name) if name != "pwsh" else Path("/pwsh"),
    )
    argv = executable_argv([str(shim), "path with spaces"], windows=True)
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert argv[0] == "/pwsh"
    assert f"'{shim}' 'path with spaces'" in script


def test_windows_cmd_resolution_prefers_sibling_powershell_shim(tmp_path, monkeypatch):
    cmd = tmp_path / "opencode.cmd"
    powershell = tmp_path / "opencode.ps1"
    cmd.touch()
    powershell.touch()
    monkeypatch.setattr("orch.platform_support.shutil.which", lambda _name: str(cmd))
    assert resolve_executable("opencode", windows=True) == powershell.resolve()


def test_viewer_unavailable_returns_manual_command(monkeypatch):
    monkeypatch.setattr("orch.platform_support.shutil.which", lambda _name: None)
    monkeypatch.delenv("TMUX", raising=False)
    result = launch_viewer("feature-one")
    assert not result.success
    assert "orch watch feature-one" in result.message


def test_notification_failure_does_not_expose_webhook_url(monkeypatch):
    def fail(*_args, **_kwargs):
        raise urllib.error.URLError("credential leaked by transport")

    monkeypatch.setattr("orch.platform_support.urllib.request.urlopen", fail)
    result = notify("title", "summary", "https://user:secret@example.invalid/hook")
    assert not result.success
    assert "secret" not in result.message
    assert "credential leaked" not in result.message


def test_batch_shim_rejects_cmd_expansion(tmp_path, monkeypatch):
    shim = tmp_path / "agent.cmd"
    shim.touch()
    monkeypatch.setattr(
        "orch.platform_support.resolve_executable", lambda name, windows=None: Path(name)
    )
    with pytest.raises(LaunchError, match="metacharacters"):
        executable_argv([str(shim), "%SECRET%"], windows=True)


def test_known_secret_in_result_is_sanitized_and_rejected(tmp_path):
    class Stub(OpenCodeAdapter):
        def capabilities(self):
            return AgentCapabilities("stub", frozenset())

        def command(self, request):
            return [sys.executable, "-c", 'print("done")']

    target = request(tmp_path)
    target.output_file.write_text("# Result\nTOPSECRET\n")
    with pytest.raises(LaunchError, match="sensitive material"):
        asyncio.run(Stub(secrets=["TOPSECRET"]).run(target))
    assert "TOPSECRET" not in target.output_file.read_text()
    assert "[REDACTED]" in target.output_file.read_text()


def test_recognizable_secret_in_result_is_sanitized_and_rejected(tmp_path):
    class Stub(OpenCodeAdapter):
        def capabilities(self):
            return AgentCapabilities("stub", frozenset())

        def command(self, request):
            return [sys.executable, "-c", 'print("done")']

    target = request(tmp_path)
    target.output_file.write_text('{"api_key":"sk-abcdefghijklmnop"}\n')
    with pytest.raises(LaunchError, match="sensitive material"):
        asyncio.run(Stub().run(target))
    content = target.output_file.read_text()
    assert "sk-abcdefghijklmnop" not in content
    assert '"api_key":"[REDACTED]"' in content
