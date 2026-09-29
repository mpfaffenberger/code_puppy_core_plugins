import asyncio
from unittest.mock import AsyncMock

import pytest

from code_puppy_core_plugins.background_agents import register_callbacks as plugin


class Owner:
    name = "main"


@pytest.fixture
def setup(monkeypatch):
    from code_puppy import agent_execution_context
    from code_puppy.messaging import run_ui
    from code_puppy.tools import agent_tools  # noqa: F401
    from code_puppy.tools import subagent_invocation

    owner = Owner()
    monkeypatch.setattr(agent_execution_context, "get_executing_agent", lambda: owner)
    monkeypatch.setattr(run_ui, "is_persistent", lambda: True)
    monkeypatch.setattr(subagent_invocation, "recursion_guard_error", lambda name: None)
    return owner, plugin.launch_background_agent


@pytest.mark.asyncio
async def test_returns_immediately_and_delivers(setup, monkeypatch):
    from code_puppy.agent_completion_inbox import pop_completion
    from code_puppy.tools import subagent_invocation
    from code_puppy.tools.agent_tools import AgentInvokeOutput

    owner, invoke = setup
    finish = asyncio.Event()

    async def run(**kwargs):
        await finish.wait()
        return AgentInvokeOutput(response="finished", agent_name="worker")

    monkeypatch.setattr(subagent_invocation, "_invoke_agent_impl", run)
    launch = await invoke(None, "worker", "do work")
    assert launch["status"] == "running"
    task = plugin._tasks[launch["task_id"]]
    assert not task.done()
    assert pop_completion(owner) is None
    finish.set()
    await task
    report = pop_completion(owner)
    assert "finished" in report
    assert launch["task_id"] in report
    assert pop_completion(owner) is None
    assert launch["task_id"] not in plugin._tasks


@pytest.mark.asyncio
async def test_failure_delivered(setup, monkeypatch):
    from code_puppy.agent_completion_inbox import pop_completion
    from code_puppy.tools import subagent_invocation

    owner, invoke = setup
    monkeypatch.setattr(
        subagent_invocation,
        "_invoke_agent_impl",
        AsyncMock(side_effect=ValueError("boom")),
    )
    launch = await invoke(None, "worker", "work")
    await plugin._tasks[launch["task_id"]]
    assert "boom" in pop_completion(owner)


@pytest.mark.asyncio
async def test_guards_and_unsupported_frontend(setup, monkeypatch):
    from code_puppy.messaging import run_ui
    from code_puppy.tools import subagent_invocation

    _, invoke = setup
    # Self-invocation is intentionally permitted by policy; only the real
    # guards below reject.
    assert "error" in await invoke(None, "worker", "work", "invalid/session")
    monkeypatch.setattr(
        subagent_invocation, "recursion_guard_error", lambda name: "denied"
    )
    assert await invoke(None, "worker", "work") == {"error": "denied"}
    monkeypatch.setattr(run_ui, "is_persistent", lambda: False)
    assert "error" in await invoke(None, "worker", "work")
    assert not plugin._tasks


@pytest.mark.asyncio
async def test_cancel_reports_and_releases_session(setup, monkeypatch):
    from code_puppy.agent_completion_inbox import pop_completion
    from code_puppy.tools import subagent_invocation

    owner, invoke = setup
    started = asyncio.Event()

    async def run(**kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(subagent_invocation, "_invoke_agent_impl", run)
    launch = await invoke(None, "worker", "work", "shared-session")
    task = plugin._tasks[launch["task_id"]]
    await started.wait()
    assert "error" in await invoke(None, "worker", "work", "shared-session")
    task.cancel()
    await task
    await asyncio.sleep(0)
    assert "cancelled" in pop_completion(owner)
    assert "shared-session" not in plugin._sessions


@pytest.mark.asyncio
async def test_publishes_detached_boundary_event(setup, monkeypatch):
    """The panel retires a background row on this event; without it the row
    (which now outlives the main turn) would tick forever in high-output mode."""
    from code_puppy import callbacks
    from code_puppy.agent_completion_inbox import pop_completion
    from code_puppy.tools import subagent_invocation
    from code_puppy.tools.agent_tools import AgentInvokeOutput

    owner, invoke = setup
    events = []

    async def capture(*args):
        events.append(args)

    async def run(**kwargs):
        return AgentInvokeOutput(
            response="ok", agent_name="worker", session_id="sess-bg"
        )

    monkeypatch.setattr(subagent_invocation, "_invoke_agent_impl", run)
    monkeypatch.setattr(callbacks, "on_post_tool_call", capture)
    launch = await invoke(None, "worker", "do work")
    await plugin._tasks[launch["task_id"]]

    assert len(events) == 1
    tool_name, tool_args, result, duration_ms, context = events[0]
    assert tool_name == "invoke_agent"
    assert tool_args == {"agent_name": "worker", "prompt": "do work"}
    assert result.session_id == "sess-bg"
    assert duration_ms >= 0
    assert context == {"detached": True}
    assert "ok" in pop_completion(owner)


@pytest.mark.asyncio
async def test_boundary_event_failure_does_not_eat_report(setup, monkeypatch):
    from code_puppy import callbacks
    from code_puppy.agent_completion_inbox import pop_completion
    from code_puppy.tools import subagent_invocation
    from code_puppy.tools.agent_tools import AgentInvokeOutput

    owner, invoke = setup

    async def run(**kwargs):
        return AgentInvokeOutput(response="ok", agent_name="worker")

    monkeypatch.setattr(subagent_invocation, "_invoke_agent_impl", run)
    monkeypatch.setattr(
        callbacks, "on_post_tool_call", AsyncMock(side_effect=RuntimeError("panel"))
    )
    launch = await invoke(None, "worker", "do work")
    await plugin._tasks[launch["task_id"]]
    assert "ok" in pop_completion(owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_boundary", [False, True])
async def test_completion_wakes_owner_before_boundary_finishes(
    setup, monkeypatch, cancel_boundary
):
    from code_puppy import callbacks
    from code_puppy.agent_completion_inbox import (
        pop_completion,
        wait_for_completion_or_input,
    )
    from code_puppy.tools import subagent_invocation
    from code_puppy.tools.agent_tools import AgentInvokeOutput

    owner, invoke = setup
    boundary_started = asyncio.Event()
    release_boundary = asyncio.Event()

    async def boundary(*args):
        boundary_started.set()
        await release_boundary.wait()

    monkeypatch.setattr(
        subagent_invocation,
        "_invoke_agent_impl",
        AsyncMock(return_value=AgentInvokeOutput(response="ok", agent_name="worker")),
    )
    monkeypatch.setattr(callbacks, "on_post_tool_call", boundary)
    waiter = asyncio.create_task(wait_for_completion_or_input(owner, asyncio.Queue()))
    launch = await invoke(None, "worker", "work", "boundary-session")
    task = plugin._tasks[launch["task_id"]]
    try:
        await asyncio.wait_for(boundary_started.wait(), 1)
        assert "completed" in await asyncio.wait_for(waiter, 1)
        assert "ok" in pop_completion(owner)
        assert pop_completion(owner) is None
        # Shutdown must still be able to find a task blocked in callbacks.
        assert plugin._tasks[launch["task_id"]] is task
        if cancel_boundary:
            await plugin._shutdown()
        else:
            release_boundary.set()
            await task
    finally:
        release_boundary.set()
        await asyncio.gather(task, return_exceptions=True)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
    assert launch["task_id"] not in plugin._tasks
    assert "boundary-session" not in plugin._sessions
    assert pop_completion(owner) is None


def test_pydantic_tool_registration():
    from pydantic_ai import Agent

    agent = Agent("test")
    from code_puppy.tools.agent_tools import register_invoke_agent

    register_invoke_agent(agent)
    tool = agent._function_toolset.tools["invoke_agent"]
    assert set(tool.function_schema.json_schema["properties"]) == {
        "agent_name",
        "prompt",
        "session_id",
        "background",
    }


@pytest.mark.asyncio
async def test_existing_tool_routes_only_background_true(monkeypatch):
    from code_puppy.tools import agent_tools
    from code_puppy.tools import subagent_invocation

    foreground = AsyncMock(return_value="foreground")
    background = AsyncMock(return_value={"status": "running"})
    monkeypatch.setattr(subagent_invocation, "_invoke_agent_impl", foreground)
    monkeypatch.setattr(plugin, "launch_background_agent", background)
    registrar = type("Registrar", (), {"tool": lambda self, func: func})()
    invoke = agent_tools.register_invoke_agent(registrar)
    assert await invoke(None, "worker", "work") == "foreground"
    assert await invoke(None, "worker", "work", background=False) == "foreground"
    background.assert_not_awaited()
    assert await invoke(None, "worker", "work", background=True) == {
        "status": "running"
    }
    background.assert_awaited_once_with(None, "worker", "work", None)
    assert foreground.await_count == 2
