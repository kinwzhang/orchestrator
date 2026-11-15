"""Independent coverage for the documentation-review OpenCode profile.

These tests are deterministic and never invoke the OpenCode CLI or a provider.
They exercise the generated deny-by-default profile, the resolved-configuration
validator and the isolated environment setup that the real integration test
depends on. The CLI-backed integration test in ``test_agents.py`` remains the
authoritative end-to-end check for tool decisions.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from orch.agents import (
    AgentRequest,
    OpenCodeAdapter,
    _verify_docs_access_profile,
    docs_access_profile,
)
from orch.platform_support import LaunchError


def docs_request(tmp_path: Path, **overrides) -> AgentRequest:
    curated = tmp_path / "docs-review"
    curated.mkdir()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    instruction = inputs / "review-docs-1.md"
    instruction.write_text("# Review curated Markdown only\n", encoding="utf-8")
    document = curated / "001-guide.md"
    document.write_text("# Delivered documentation\n", encoding="utf-8")
    values = {
        "role": "docs_reviewer",
        "logical_id": "review-docs",
        "attempt_id": "a1",
        "instruction_file": instruction,
        "input_files": (document,),
        "cwd": curated,
        "output_file": tmp_path / "verdict-docs-1.md",
        "model": "local/fake",
        "log_dir": tmp_path / "logs",
    }
    return AgentRequest(**(values | overrides))


def write_profile(tmp_path: Path, request: AgentRequest) -> tuple[Path, dict]:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(exist_ok=True)
    profile = docs_access_profile(request, profile_dir)
    config_path = profile_dir / "opencode.json"
    config_path.write_text(json.dumps(profile), encoding="utf-8")
    return config_path, profile


def tool_action(tool: str) -> str:
    """Map profile tool names to resolved CLI permission action names."""

    return {"bash": "shell", "task": "subagent"}.get(tool, tool)


def resolved_rules(profile: dict) -> list[dict]:
    """Shape the generated profile like a resolved OpenCode rule list.

    Ordering mirrors what the installed CLI reports: tool denials first, then the
    wildcard denial, then each action's deny-all rule followed by exact grants.
    """

    rules = [
        {"action": tool_action(tool), "resource": "*", "effect": "deny"}
        for tool in profile["tools"]
    ]
    rules.append({"action": "*", "resource": "*", "effect": "deny"})
    for action in ("read", "edit", "external_directory"):
        for resource, effect in profile["permission"][action].items():
            rules.append({"action": action, "resource": resource, "effect": effect})
    return rules


def document_source(config_path: Path, profile: dict) -> dict:
    return {
        "type": "document",
        "path": str(config_path),
        "info": {
            "permissions": resolved_rules(profile),
            "plugins": profile["plugin"],
            "mcp": profile["mcp"],
        },
    }


def test_profile_allows_only_approved_markdown_and_exact_verdict_write(tmp_path):
    request = docs_request(tmp_path)
    _, profile = write_profile(tmp_path, request)
    permissions = profile["permission"]

    approved_reads = {
        str(request.instruction_file.resolve()),
        *(str(path.resolve()) for path in request.input_files),
        str(request.output_file.resolve()),
    }
    verdict = str(request.output_file.resolve())

    assert permissions["*"] == "deny"
    assert set(permissions["read"]) == {"*", *approved_reads}
    assert permissions["read"]["*"] == "deny"
    assert all(permissions["read"][path] == "allow" for path in approved_reads)
    assert set(permissions["edit"]) == {"*", verdict}
    assert permissions["edit"] == {"*": "deny", verdict: "allow"}
    assert permissions["external_directory"]["*"] == "deny"
    assert profile["tools"] and all(value is False for value in profile["tools"].values())
    assert profile["plugin"] == [] and profile["mcp"] == {}


def test_profile_excludes_source_and_unapproved_markdown(tmp_path):
    request = docs_request(tmp_path)
    source = request.cwd / "src" / "private.py"
    source.parent.mkdir()
    source.write_text("LOCAL_ONLY_SOURCE_SENTINEL", encoding="utf-8")
    unapproved = request.cwd / "002-extra.md"
    unapproved.write_text("# Not supplied for review\n", encoding="utf-8")
    _, profile = write_profile(tmp_path, request)

    grants = {
        resource
        for action in ("read", "edit")
        for resource, effect in profile["permission"][action].items()
        if effect == "allow"
    }
    denied = {str(source.resolve()), str(unapproved.resolve())}
    assert denied.isdisjoint(grants)
    assert profile["permission"]["read"]["*"] == "deny"
    assert profile["permission"]["edit"]["*"] == "deny"


def test_validator_accepts_exact_profile_after_inherited_configuration(tmp_path, monkeypatch):
    request = docs_request(tmp_path)
    config_path, profile = write_profile(tmp_path, request)
    inherited = {
        "type": "document",
        "path": str(tmp_path / "synthetic-inherited" / "opencode.json"),
        "info": {
            "permissions": [{"action": "read", "resource": "*", "effect": "allow"}],
            "plugins": [],
            "mcp": {},
        },
    }
    report = [
        inherited,
        {"type": "directory", "path": str(tmp_path / "profile")},
        document_source(config_path, profile),
    ]
    monkeypatch.setattr("orch.agents._capture", lambda *a, **k: json.dumps(report))

    # Later-loaded inherited settings are refused; earlier ones are not a bypass
    # because the exact profile denies remain the effective final state.
    _verify_docs_access_profile("opencode", config_path, profile_dir(tmp_path), {})


def profile_dir(tmp_path: Path) -> Path:
    path = tmp_path / "profile"
    path.mkdir(exist_ok=True)
    return path


def test_validator_refuses_same_profile_reported_after_itself(tmp_path, monkeypatch):
    request = docs_request(tmp_path)
    config_path, profile = write_profile(tmp_path, request)
    source = document_source(config_path, profile)
    # Observed standalone provenance: the generated file is listed once as the
    # project configuration and again as the explicitly selected file.
    report = [
        {"type": "directory", "path": str(tmp_path / "profile" / "config-dir")},
        source,
        json.loads(json.dumps(source)),
    ]
    monkeypatch.setattr("orch.agents._capture", lambda *a, **k: json.dumps(report))

    with pytest.raises(LaunchError, match="later OpenCode config adds an allow rule"):
        _verify_docs_access_profile("opencode", config_path, profile_dir(tmp_path), {})


def test_validator_refuses_inherited_allow_loaded_after_the_profile(tmp_path, monkeypatch):
    request = docs_request(tmp_path)
    config_path, profile = write_profile(tmp_path, request)
    later = {
        "type": "document",
        "path": str(tmp_path / "synthetic-later" / "opencode.json"),
        "info": {
            "permissions": [{"action": "read", "resource": "*", "effect": "allow"}],
            "plugins": [],
            "mcp": {},
        },
    }
    report = [
        {"type": "directory", "path": str(tmp_path / "profile")},
        document_source(config_path, profile),
        later,
    ]
    monkeypatch.setattr("orch.agents._capture", lambda *a, **k: json.dumps(report))

    with pytest.raises(LaunchError, match="later OpenCode config adds an allow rule"):
        _verify_docs_access_profile("opencode", config_path, profile_dir(tmp_path), {})


def test_validator_refuses_later_narrow_allow_rule(tmp_path, monkeypatch):
    request = docs_request(tmp_path)
    config_path, profile = write_profile(tmp_path, request)
    additional = tmp_path / "not-supplied" / "extra.md"
    later = {
        "type": "document",
        "path": str(tmp_path / "synthetic-narrow" / "opencode.json"),
        "info": {
            "permissions": [
                {"action": "read", "resource": str(additional.resolve()), "effect": "allow"}
            ],
            "plugins": [],
            "mcp": {},
        },
    }
    report = [
        {"type": "directory", "path": str(tmp_path / "profile")},
        document_source(config_path, profile),
        later,
    ]
    monkeypatch.setattr("orch.agents._capture", lambda *a, **k: json.dumps(report))

    # A single extra exact Markdown path is still a widening rule after the denies.
    with pytest.raises(LaunchError, match="later OpenCode config adds an allow rule"):
        _verify_docs_access_profile("opencode", config_path, profile_dir(tmp_path), {})


def test_profile_refuses_non_markdown_inputs_and_output(tmp_path):
    request = docs_request(tmp_path)
    with pytest.raises(LaunchError, match="allows Markdown files only"):
        docs_access_profile(
            replace(request, output_file=tmp_path / "verdict.txt"), profile_dir(tmp_path)
        )
    text = tmp_path / "inputs" / "extra.txt"
    text.write_text("# not Markdown\n", encoding="utf-8")
    with pytest.raises(LaunchError, match="allows Markdown files only"):
        docs_access_profile(
            replace(request, input_files=(*request.input_files, text)), profile_dir(tmp_path)
        )


def test_validator_refuses_later_plugins_or_mcp_servers(tmp_path, monkeypatch):
    request = docs_request(tmp_path)
    config_path, profile = write_profile(tmp_path, request)
    base = [
        {"type": "directory", "path": str(tmp_path / "profile")},
        document_source(config_path, profile),
    ]
    for later_info in (
        {"permissions": [], "plugins": ["unexpected-plugin"], "mcp": {}},
        {"permissions": [], "plugins": [], "mcp": {"unexpected": {}}},
    ):
        report = [*base, {"type": "document", "path": str(tmp_path / "later.json"),
                           "info": later_info}]
        monkeypatch.setattr(
            "orch.agents._capture", lambda *a, _report=report, **k: json.dumps(_report)
        )
        with pytest.raises(LaunchError, match="enables plugins or MCP tools"):
            _verify_docs_access_profile("opencode", config_path, profile_dir(tmp_path), {})


def test_validator_refuses_report_without_the_generated_profile(tmp_path, monkeypatch):
    request = docs_request(tmp_path)
    config_path, profile = write_profile(tmp_path, request)
    report = [
        {"type": "directory", "path": str(tmp_path / "profile")},
        {"type": "document", "path": str(tmp_path / "other" / "opencode.json"),
         "info": {"permissions": resolved_rules(profile), "plugins": [], "mcp": {}}},
    ]
    monkeypatch.setattr("orch.agents._capture", lambda *a, **k: json.dumps(report))

    with pytest.raises(LaunchError, match="did not load the docs-role access policy"):
        _verify_docs_access_profile("opencode", config_path, profile_dir(tmp_path), {})


def test_docs_environment_roots_the_profile_and_verifies_it(tmp_path, monkeypatch):
    request = docs_request(tmp_path)
    captured = []

    def fake_capture(argv, *, cwd=None, env=None):
        captured.append(argv)
        config_path = Path(cwd) / "opencode.json"
        profile = json.loads(config_path.read_text(encoding="utf-8"))
        report = [
            {"type": "directory", "path": str(Path(cwd) / "config-dir")},
            document_source(config_path, profile),
        ]
        return json.dumps(report)

    monkeypatch.setattr("orch.agents._capture", fake_capture)
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", "synthetic inline override")

    adapter = OpenCodeAdapter(executable="opencode")
    with adapter.request_environment(request) as (cwd, env):
        assert captured == [["opencode", "debug", "config"]]
        assert Path(cwd).is_dir()
        assert Path(cwd).parent == Path(request.cwd).resolve()
        assert Path(cwd).name.startswith(".orch-opencode-docs-")
        assert env["OPENCODE_CONFIG"] == str(Path(cwd) / "opencode.json")
        assert Path(env["OPENCODE_CONFIG_DIR"]).is_dir()
        assert Path(env["OPENCODE_CONFIG_DIR"]).parent == Path(cwd)
        assert env["OPENCODE_CONFIG_DIR"].startswith(str(request.cwd))
        assert "OPENCODE_CONFIG_CONTENT" not in env
        assert Path(env["OPENCODE_CONFIG"]).is_relative_to(Path(cwd))
        profile = json.loads(Path(env["OPENCODE_CONFIG"]).read_text(encoding="utf-8"))
        assert profile["permission"]["edit"] == {
            "*": "deny",
            str(request.output_file.resolve()): "allow",
        }
