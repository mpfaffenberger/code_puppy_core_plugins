"""Real dispatch keeps starts private and hook-mutated metadata non-authoritative."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tool_manager import ToolManager

from code_puppy import callbacks, pydantic_patches
from code_puppy_core_plugins.acp import state
from code_puppy_core_plugins.acp.bridge import EventBridge


@pytest.mark.parametrize("mutate_identity", [False, True])
@pytest.mark.parametrize(
    "case", ["ok", "blocked-hook", "blocked-final", "pre-cancel", "execution-cancel"]
)
async def test_actual_dispatch_privacy_and_identity(monkeypatch, mutate_identity, case):
    try:
        from code_puppy.tool_identity import current_tool_identity
    except ImportError:
        pytest.skip("requires core's isolated hook identity contract")
    try:
        from code_puppy import final_guard
    except ImportError:
        pytest.skip(
            "requires runtime-ID-capable core's terminal outcomes / final guards"
        )
    monkeypatch.setattr(
        callbacks, "_callbacks", {phase: [] for phase in callbacks._callbacks}
    )
    if final_guard is not None:
        monkeypatch.setattr(final_guard, "_guards", ())
    monkeypatch.setattr(
        "code_puppy.messaging.emit_warning", lambda *args, **kwargs: None
    )
    wrappers = {
        "execute_tool_call": "_original_execute_tool_call",
        "get_tool_def": "_original_get_tool_def",
        "validate_tool_call": "_original_validate_tool_call",
        "validate_output_tool_call": "_original_validate_output_tool_call",
    }
    for name, original in wrappers.items():
        method = getattr(ToolManager, name)
        while getattr(method, "__closure__", None):
            cells = dict(
                zip(
                    method.__code__.co_freevars,
                    (cell.cell_contents for cell in method.__closure__),
                )
            )
            if original not in cells:
                break
            method = cells[original]
        monkeypatch.setattr(ToolManager, name, method)
    assert pydantic_patches.patch_tool_call_callbacks()
    updates = []

    async def session_update(session_id, update):
        updates.append(update)

    state.set_connection(
        SimpleNamespace(session_update=session_update), asyncio.get_running_loop()
    )
    state.begin_run("privacy-dispatch")
    entered = asyncio.Event()
    calls = []
    if mutate_identity:

        def mutate(name, args, context):
            context["tool_call_id"] = "not-authoritative"
            args["command"] += "-mutated"

        callbacks.register_callback("pre_tool_call", mutate)
    bridge = EventBridge()
    bridge.register()
    if mutate_identity:
        callbacks.unregister_callback("post_tool_call", bridge._on_post_tool_call)

        def mutate_terminal(name, args, result, duration, context):
            context["tool_call_id"] = "not-authoritative-terminal"
            context["outcome"] = "ok"

        callbacks.register_callback("post_tool_call", mutate_terminal)
        callbacks.register_callback("post_tool_call", bridge._on_post_tool_call)

    async def operation(command: str):
        calls.append(command)
        if case == "execution-cancel":
            entered.set()
            await asyncio.Event().wait()
        return "safe result"

    async def hook(*args):
        if case == "blocked-hook":
            return {"blocked": True}
        if case == "pre-cancel":
            entered.set()
            await asyncio.Event().wait()

    callbacks.register_callback("pre_tool_call", hook)
    if case == "blocked-final":
        final_guard.register_final_guard(lambda call: {"blocked": True})

    def model(messages, info):
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "operation",
                        {"command": "private-sentinel"},
                        tool_call_id="authoritative",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    try:
        task = asyncio.create_task(
            Agent(FunctionModel(model), tools=[operation]).run("run")
        )
        if case.endswith("cancel"):
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel("original cancellation")
            with pytest.raises(asyncio.CancelledError, match="original cancellation"):
                await task
        else:
            await task
        assert updates[0].title == "operation"
        assert updates[0].raw_input == {} and not updates[0].locations
        assert current_tool_identity() is None
        assert bool(calls) == (case in {"ok", "execution-cancel"})
        if case != "ok":
            assert "private-sentinel" not in repr(
                [update.model_dump() for update in updates]
            )
        if case == "pre-cancel":
            # Older cores lack pre-execution cancellation terminals.
            if len(updates) == 1:
                return
        assert len(updates) == 2
        assert updates[0].tool_call_id == updates[1].tool_call_id
        assert updates[1].status == ("completed" if case == "ok" else "failed")
        assert state.current_tool_call() is None
        if case == "ok":
            assert calls == [
                "private-sentinel" + ("-mutated" if mutate_identity else "")
            ]
            assert updates[1].raw_input == {"command": calls[0]}
    finally:
        bridge.unregister()
        state.end_run()
        state.set_connection(None, None)
