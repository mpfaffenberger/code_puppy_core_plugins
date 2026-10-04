"""Shared API registration, serialization, and fail-closed policy regressions."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic_ai import Agent

from code_puppy_core_plugins.computer_use import commands, register_callbacks, tools
from code_puppy_core_plugins.computer_use.backend import MacOSBackend
from code_puppy_core_plugins.computer_use.backend_types import ComputerUseError
from code_puppy_core_plugins.computer_use.batch import run_batch
from code_puppy_core_plugins.computer_use.policy import PolicyStore
from code_puppy_core_plugins.computer_use.windows_backend import WindowsBackend
from code_puppy_core_plugins.computer_use.windows_runtime import WindowsRuntime


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_identical_twelve_tools_and_schema_generation(monkeypatch, platform):
    monkeypatch.setattr(register_callbacks.sys, "platform", platform)
    monkeypatch.setattr(register_callbacks.policy_store, "is_enabled", lambda: True)
    definitions = register_callbacks._register_tools()
    assert len(definitions) == 12
    assert {entry["name"] for entry in definitions} == set(tools.REGISTRARS)
    assert set(register_callbacks._register_agent_tools()) == set(tools.REGISTRARS)
    agent = Agent(model="test")
    for definition in definitions:
        definition["register_func"](agent)
    assert set(agent._function_toolset.tools) == set(tools.REGISTRARS)
    for tool in agent._function_toolset.tools.values():
        assert tool.function_schema.json_schema["type"] == "object"


@pytest.mark.parametrize(
    "method",
    [
        "snapshot",
        "get_app_state",
        "click",
        "click_pixel",
        "set_value",
        "perform_action",
        "select_text",
        "press_key",
        "type_text",
        "scroll_pages",
        "drag_pixel",
        "screenshot",
        "require_state",
        "invalidate_state",
    ],
)
def test_both_backends_implement_dispatch_contract(method):
    assert callable(getattr(MacOSBackend, method))
    assert callable(getattr(WindowsBackend, method))


@pytest.mark.parametrize(
    "document",
    [
        "[]",
        "null",
        "true",
        "broken",
        '{"enabled":true,"denied":4}',
        '{"enabled":true,"denied":[null]}',
    ],
)
def test_malformed_policy_fails_closed(tmp_path, document):
    path = tmp_path / "policy.json"
    path.write_text(document)
    policy = PolicyStore(path)
    assert not policy.is_enabled()
    with pytest.raises(ComputerUseError):
        policy.require_enabled()


def test_command_permission_failure_reported(monkeypatch):
    messages = []
    monkeypatch.setattr(commands, "emit_error", messages.append)
    monkeypatch.setattr(
        commands.policy_store,
        "set_enabled",
        Mock(side_effect=ComputerUseError("ACL failure")),
    )
    assert commands.handle_command("/computer-use enable", "computer-use") is True
    assert messages == ["ACL failure"]
    assert commands.handle_command("/other", "other") is None


def test_failed_batch_invalidates_and_stops():
    # A backend without the optional Windows batch scope keeps legacy behavior.
    backend = Mock(spec=["require_state", "click", "invalidate_state", "type_text"])
    backend.require_state.return_value = SimpleNamespace(application="test")
    backend.click.return_value = {"success": False, "error": "failure"}
    result = run_batch(
        backend,
        "revision",
        [
            {"action": "click", "element_id": 1},
            {"action": "type_text", "text": "must not type"},
        ],
        Mock(),
    )
    assert not result["success"]
    backend.invalidate_state.assert_called_once()
    backend.click.assert_called_once_with("revision", consume=False, element_id=1)
    backend.type_text.assert_not_called()


@pytest.mark.asyncio
async def test_windows_requests_are_serial_and_leave_event_loop_free(monkeypatch):
    runtime = WindowsRuntime()
    runtime._backend = SimpleNamespace(states=Mock())
    entered = threading.Event()
    release = threading.Event()
    calls = []
    monkeypatch.setattr(tools, "backend", runtime)

    def first():
        calls.append("first-start")
        entered.set()
        assert release.wait(3)
        calls.append("first-end")
        return {"success": True}

    def second():
        calls.append("second")
        return {"success": True}

    try:
        task = asyncio.create_task(tools._call(first))
        assert await asyncio.to_thread(entered.wait, 3)
        other = asyncio.create_task(tools._call(second))
        await asyncio.sleep(0.02)
        assert calls == ["first-start"]
        release.set()
        await asyncio.gather(task, other)
        assert calls == ["first-start", "first-end", "second"]
    finally:
        release.set()
        runtime._executor.shutdown()


@pytest.mark.asyncio
async def test_cancellation_reaches_worker_and_invalidates_state(monkeypatch):
    runtime = WindowsRuntime()
    runtime._backend = SimpleNamespace(states=Mock())
    entered = threading.Event()
    done = threading.Event()
    monkeypatch.setattr(tools, "backend", runtime)

    def work():
        entered.set()
        assert runtime._backend.cancelled.wait(3)
        done.set()
        return {"success": False}

    try:
        task = asyncio.create_task(tools._call(work))
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(done.wait, 3)
        await tools._call(lambda: {"success": True})  # subsequent requests may proceed
        runtime._backend.states.clear.assert_called()
    finally:
        runtime._executor.shutdown()
