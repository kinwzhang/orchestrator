import asyncio
import base64
import contextlib
import json
import shutil
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from orch.agents import (
    AgentCapabilities,
    AgentRequest,
    CodexAdapter,
    OpenCodeAdapter,
    PreflightError,
    _verify_docs_access_profile,
    docs_access_profile,
    validate_launch_paths,
)
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


def test_missing_mandatory_input_fails_preflight_with_exact_path(tmp_path):
    probed = []

    class Stub(OpenCodeAdapter):
        def capabilities(self):
            probed.append(True)
            raise AssertionError("capability probe must not run before input validation")

    missing = tmp_path / "handover" / "previous.md"
    target = request(tmp_path, input_files=(missing,))
    with pytest.raises(PreflightError) as failure:
        asyncio.run(Stub().run(target))
    assert failure.value.input_kind == "input"
    assert failure.value.path == str(missing)
    assert str(missing) in str(failure.value)
    assert "missing" in str(failure.value)
    assert probed == []
    assert not target.log_dir.exists()


def test_preflight_categories_name_instruction_and_output(tmp_path):
    absent = tmp_path / "absent.md"
    with pytest.raises(PreflightError) as failure:
        validate_launch_paths(absent, (), tmp_path / "handover.md", tmp_path / "logs")
    assert failure.value.input_kind == "instruction"
    assert failure.value.path == str(absent)

    with pytest.raises(PreflightError) as failure:
        validate_launch_paths(request(tmp_path).instruction_file, (), tmp_path / "handover.json",
                              tmp_path / "logs")
    assert failure.value.input_kind == "output"

    with pytest.raises(PreflightError) as failure:
        validate_launch_paths(request(tmp_path).instruction_file, (), tmp_path / "handover.md", None)
    assert failure.value.input_kind == "log_dir"


def test_post_launch_secret_rejection_is_not_a_preflight_failure(tmp_path):
    class Stub(OpenCodeAdapter):
        def capabilities(self):
            return AgentCapabilities("stub", frozenset())

        def command(self, request):
            return [sys.executable, "-c", "pass"]

    target = request(tmp_path)
    target.output_file.write_text("# Result\nTOPSECRET\n")
    with pytest.raises(LaunchError) as failure:
        asyncio.run(Stub(secrets=["TOPSECRET"]).run(target))
    assert not isinstance(failure.value, PreflightError)


def docs_request(tmp_path, **overrides):
    cwd = tmp_path / "curated"
    cwd.mkdir()
    instruction = tmp_path / "attempt.md"
    instruction.write_text("Review only curated Markdown.")
    document = cwd / "guide.md"
    document.write_text("Curated project documentation.")
    return request(
        tmp_path,
        role="docs_reviewer",
        instruction_file=instruction,
        input_files=(document,),
        cwd=cwd,
        output_file=tmp_path / "verdict-docs.md",
        **overrides,
    )


def test_docs_access_profile_is_deny_by_default_and_file_scoped(tmp_path):
    target = docs_request(tmp_path)
    profile_cwd = target.cwd / "isolated-profile"
    profile_cwd.mkdir()
    profile = docs_access_profile(target, profile_cwd)
    permissions = profile["permission"]
    allowed = {
        str(target.instruction_file.resolve()),
        *(str(path.resolve()) for path in target.input_files),
        str(target.output_file.resolve()),
    }
    assert permissions["*"] == "deny"
    assert permissions["read"] == {"*": "deny", **dict.fromkeys(allowed, "allow")}
    assert permissions["edit"] == {
        "*": "deny",
        str(target.output_file.resolve()): "allow",
    }
    assert permissions["external_directory"] == {
        "*": "deny",
        **dict.fromkeys(
            {str(Path(path).parent / "**") for path in allowed}, "allow"
        ),
    }
    assert all(value is False for value in profile["tools"].values())
    assert profile["plugin"] == []


def test_docs_access_profile_rejects_repo_cwd_and_escaped_input(tmp_path):
    target = docs_request(tmp_path)
    (target.cwd / ".git").mkdir()
    with pytest.raises(LaunchError, match="outside Git checkouts"):
        docs_access_profile(target)

    outside = tmp_path / "outside.md"
    outside.write_text("Not copied into the curated directory.")
    (target.cwd / ".git").rmdir()
    target.input_files = (outside,)
    with pytest.raises(LaunchError, match="copied into its curated"):
        docs_access_profile(target)


def test_docs_access_preflight_rejects_missing_open_code_deny(tmp_path, monkeypatch):
    target = docs_request(tmp_path)
    profile_cwd = target.cwd / "isolated-profile"
    profile_cwd.mkdir()
    config_path = profile_cwd / "opencode.json"
    config_path.write_text(
        json.dumps(docs_access_profile(target, profile_cwd)),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "orch.agents._capture",
        lambda *_args, **_kwargs: json.dumps(
            [{"type": "document", "path": str(config_path), "info": {"permissions": []}}]
        ),
    )
    with pytest.raises(LaunchError, match="omitted a required docs-role deny rule"):
        _verify_docs_access_profile("opencode", config_path, profile_cwd, {})


@pytest.mark.skipif(shutil.which("opencode") is None, reason="OpenCode CLI is not installed")
def test_opencode_docs_policy_denies_source_read_and_allows_verdict(tmp_path):
    """Exercise v2 tool permissions with a local fake provider; no paid API is used."""
    source_secret = "LOCAL_ONLY_SOURCE_SENTINEL"
    verdict = "# Verdict\n\n## Summary\nLocal permission test passed.\n"

    class FakeProvider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self.server.requests_seen.append(body)
            tool_names = [
                item.get("function", {}).get("name") for item in body.get("tools", [])
            ]
            if "read" in tool_names and not self.server.read_requested:
                self.server.read_requested = True
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-source-read",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": json.dumps({"path": str(self.server.source)}),
                            },
                        }
                    ],
                }
                finish = "tool_calls"
            elif "write" in tool_names and not self.server.verdict_written:
                self.server.verdict_written = True
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-verdict-write",
                            "type": "function",
                            "function": {
                                "name": "write",
                                "arguments": json.dumps(
                                    {"path": str(self.server.output), "content": verdict}
                                ),
                            },
                        }
                    ],
                }
                finish = "tool_calls"
            else:
                delta = {
                    "role": "assistant",
                    "content": "The source read was denied and the verdict was written.",
                }
                finish = "stop"
            chunks = [
                {
                    "id": "chatcmpl-local-test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "fake",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {
                    "id": "chatcmpl-local-test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "fake",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                },
                "[DONE]",
            ]
            response = "".join(
                "data: " + (chunk if isinstance(chunk, str) else json.dumps(chunk)) + "\n\n"
                for chunk in chunks
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    server.requests_seen = []
    server.read_requested = False
    server.verdict_written = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        target = docs_request(tmp_path, model="local/fake")
        source = target.cwd / "src" / "private.py"
        source.parent.mkdir()
        source.write_text(source_secret, encoding="utf-8")
        server.source = source
        server.output = target.output_file

        class LocalProviderAdapter(OpenCodeAdapter):
            @contextlib.contextmanager
            def request_environment(self, request):
                with super().request_environment(request) as (cwd, env):
                    config_path = Path(env["OPENCODE_CONFIG"])
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                    config.update(
                        {
                            "autoupdate": False,
                            "update": "disable",
                            "share": "disabled",
                            "warming": False,
                            "small_model": "local/fake",
                            "provider": {
                                "local": {
                                    "npm": "@ai-sdk/openai-compatible",
                                    "name": "Local permission-test provider",
                                    "options": {
                                        "baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                                        "apiKey": "local-test-only",
                                    },
                                    "models": {
                                        "fake": {
                                            "name": "Local fake model",
                                            "tool_call": True,
                                            "limit": {"context": 8192, "output": 1024},
                                        }
                                    },
                                }
                            },
                        }
                    )
                    config_path.write_text(json.dumps(config), encoding="utf-8")
                    yield cwd, env

        result = asyncio.run(LocalProviderAdapter().run(target))
        all_messages = [
            message
            for request_body in server.requests_seen
            for message in request_body.get("messages", [])
        ]
        serialized = json.dumps(all_messages) + result.stdout_log.read_text() + result.stderr_log.read_text()
        assert result.exit_code == 0
        assert server.read_requested
        assert any("permission.rejected" in json.dumps(message) for message in all_messages)
        assert server.verdict_written
        assert target.output_file.read_text(encoding="utf-8") == verdict
        assert source_secret not in serialized
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
