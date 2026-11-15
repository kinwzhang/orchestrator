"""Deterministic invocation-identity capture for round-two acceptance."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from orch.agents import (
    AgentCapabilities,
    AgentRequest,
    CodexAdapter,
    OpenCodeAdapter,
    PreflightError,
)
from orch.contracts import ModelSpec
from orch.workflow import record_invocation_identity


def request(tmp_path, **overrides):
    instruction = tmp_path / "instruction.md"
    instruction.write_text("# Task", encoding="utf-8")
    values = {
        "role": "worker",
        "logical_id": "w01",
        "attempt_id": "w01-1-1",
        "instruction_file": instruction,
        "output_file": tmp_path / "handover.md",
        "cwd": tmp_path,
        "model": "provider/coder",
        "log_dir": tmp_path / "logs",
    }
    return AgentRequest(**(values | overrides))


class StubRunner:
    """Stand-in process runner that never creates a real process."""

    def __init__(self, order=None):
        self.calls = []
        self.order = order if order is not None else []

    async def run(self, command, cwd, *, timeout, log_paths, on_event, env):
        self.calls.append(list(command))
        self.order.append("runner")
        return SimpleNamespace(
            exit_code=0,
            duration_seconds=0.5,
            stdout_log=log_paths[0],
            stderr_log=log_paths[1],
            reason=None,
        )

    def redact(self, text):
        return text


class StubOpenCode(OpenCodeAdapter):
    def __init__(self, runner):
        super().__init__(runner=runner)

    def capabilities(self):
        return AgentCapabilities("stub", frozenset())

    def command(self, request):
        return [sys.executable, "-c", "pass"]


class StubCodex(CodexAdapter):
    def __init__(self, runner):
        super().__init__(runner=runner)

    def capabilities(self):
        return AgentCapabilities("stub", frozenset())

    def command(self, request):
        return [sys.executable, "-c", "pass"]


def run_adapter(adapter, target, events):
    def on_event(event):
        events.append(dict(event))

    return asyncio.run(adapter.run(target, on_event=on_event))


def identity_events(events):
    return [event for event in events if event.get("kind") == "invocation_identity"]


def test_opencode_identity_is_captured_before_execution(tmp_path):
    order = []
    runner = StubRunner(order)
    events = []

    result = run_adapter(StubOpenCode(runner), request(
        tmp_path, model="opencode-go/deepseek-v4.1-flash"), events)

    assert result.exit_code == 0
    captured = identity_events(events)
    assert len(captured) == 1
    assert captured[0]["logical_id"] == "w01"
    assert captured[0]["attempt_id"] == "w01-1-1"
    assert captured[0]["adapter"] == "opencode"
    assert captured[0]["provider"] == "opencode-go"
    assert captured[0]["model"] == "opencode-go/deepseek-v4.1-flash"
    assert order == ["runner"]


def test_two_attempts_with_different_models_keep_distinct_evidence(tmp_path):
    runner = StubRunner()
    events = []
    first = request(tmp_path, attempt_id="w01-1-1", model="alpha/one")
    second = request(tmp_path, attempt_id="w01-1-2", model="beta/two")
    run_adapter(StubOpenCode(runner), first, events)
    run_adapter(StubOpenCode(runner), second, events)

    captured = identity_events(events)
    assert [(event["attempt_id"], event["provider"], event["model"])
            for event in captured] == [
        ("w01-1-1", "alpha", "alpha/one"),
        ("w01-1-2", "beta", "beta/two"),
    ]


@pytest.mark.parametrize("model", ["unqualified", "a/b/c", "/leading", "trailing/"])
def test_unqualified_or_ambiguous_provider_is_unknown_with_known_model(tmp_path, model):
    events = []
    runner = StubRunner()
    run_adapter(StubOpenCode(runner), request(tmp_path, model=model), events)

    captured = identity_events(events)
    assert len(captured) == 1
    assert captured[0]["provider"] == "unknown"
    assert captured[0]["model"] == model


def test_codex_identity_names_its_adapter_and_provider(tmp_path):
    events = []
    runner = StubRunner()
    run_adapter(StubCodex(runner), request(tmp_path, model="gpt-test"), events)

    captured = identity_events(events)
    assert len(captured) == 1
    assert captured[0]["adapter"] == "codex"
    assert captured[0]["provider"] == "codex"
    assert captured[0]["model"] == "gpt-test"


def test_preflight_failure_occurs_before_identity_capture(tmp_path):
    events = []
    runner = StubRunner()
    missing = tmp_path / "missing.md"
    with pytest.raises(PreflightError):
        run_adapter(StubOpenCode(runner),
                    request(tmp_path, input_files=(missing,)), events)

    assert identity_events(events) == []
    assert runner.calls == []


def test_recording_is_idempotent_and_conflicts_become_explicit(tmp_path):
    info = {}
    event = {
        "kind": "invocation_identity",
        "logical_id": "w01",
        "attempt_id": "w01-1-1",
        "adapter": "opencode",
        "provider": "alpha",
        "model": "alpha/one",
    }
    recorded = record_invocation_identity(info, "w01", "w01-1-1", event)
    assert recorded["evidence_status"] == "recorded"
    assert record_invocation_identity(info, "w01", "w01-1-1", event) == recorded
    assert info["invocation_attempts"]["w01-1-1"]["provider"] == "alpha"

    conflicting = dict(event, provider="beta", model="beta/two")
    updated = record_invocation_identity(info, "w01", "w01-1-1", conflicting)
    assert updated["evidence_status"] == "conflicting"


def test_recording_never_attaches_a_late_event_to_another_attempt(tmp_path):
    info = {"attempt_id": "w01-1-2"}
    event = {
        "kind": "invocation_identity",
        "logical_id": "w01",
        "attempt_id": "w01-1-1",
        "adapter": "opencode",
        "provider": "alpha",
        "model": "alpha/one",
    }
    assert record_invocation_identity(info, "w01", "w01-1-2", event) is None
    assert "invocation_attempts" not in info

    other_agent = dict(event, logical_id="w02", attempt_id="w01-1-2")
    assert record_invocation_identity(info, "w01", "w01-1-2", other_agent) is None
    assert "invocation_attempts" not in info


def test_terminal_control_text_is_normalized_in_evidence():
    info = {}
    event = {
        "kind": "invocation_identity",
        "logical_id": "w01",
        "attempt_id": "w01-1-1",
        "adapter": "opencode\x1b[31m",
        "provider": "alpha\x07",
        "model": "alpha/\x1b]0;title\x07one",
    }
    recorded = record_invocation_identity(info, "w01", "w01-1-1", event)
    for field in ("adapter", "provider", "model"):
        assert "\x1b" not in recorded[field]
        assert "\x07" not in recorded[field]


def test_model_identity_is_independent_of_reasoning_variant(tmp_path):
    events = []
    runner = StubRunner()
    run_adapter(StubOpenCode(runner), request(
        tmp_path, model=ModelSpec(model="alpha/one", reasoning="high")), events)

    captured = identity_events(events)
    assert len(captured) == 1
    assert captured[0]["provider"] == "alpha"
    assert captured[0]["model"] == "alpha/one"
