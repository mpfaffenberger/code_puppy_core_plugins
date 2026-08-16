"""End-to-end tests for hook-context composition and final spill bounding."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field, field_serializer
from pydantic_ai import Agent
from pydantic_ai._agent_graph import CallToolsNode
from pydantic_ai.tool_manager import ToolManager
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from code_puppy import callbacks, config
from code_puppy.pydantic_patches import patch_tool_call_callbacks
from code_puppy_core_plugins.spill import register_callbacks as spill

pytestmark = pytest.mark.skipif(
    not hasattr(callbacks, "on_final_tool_result")
    or not hasattr(callbacks, "_register_terminal_callback"),
    reason="requires coordinated final-result and terminal callback phases",
)

_NOTICE = "Full output stored at:"
_CAP = 1500


class _ExcludedOutput(BaseModel):
    visible: str = "ok"
    secret: str = Field(exclude=True)


class _RedactedOutput(BaseModel):
    visible: str = "ok"
    secret: str

    @field_serializer("secret")
    def redact_secret(self, value: str) -> str:
        _ = value
        return "[REDACTED]"


@pytest.fixture(autouse=True)
def _spill_root(tmp_path):
    root = tmp_path / "spills"
    config.set_value(spill.ROOT_KEY, str(root))
    config.set_value(spill.MAX_INLINE_KEY, str(_CAP))
    config.set_value(spill.PREVIEW_KEY, "100")
    spill._reset_state()
    yield root
    spill._reset_state()


async def _run_tool(output, hook_context: str) -> str:
    seen = {}

    def model_function(messages, info):
        _ = info
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("structured_tool", {})])
        seen["response"] = returns[-1].model_response_str()
        return ModelResponse(parts=[TextPart("done")])

    def add_context(tool_name, tool_args, context=None):
        _ = tool_name, tool_args, context
        return {"context_message": hook_context}

    original_execute_tool_call = ToolManager.execute_tool_call
    original_get_tool_def = ToolManager.get_tool_def
    original_validate_tool_call = ToolManager.validate_tool_call
    original_validate_output_tool_call = ToolManager.validate_output_tool_call
    original_handle_tool_calls = CallToolsNode._handle_tool_calls
    callbacks.register_callback("pre_tool_call", add_context)
    patch_tool_call_callbacks()
    try:
        agent = Agent(FunctionModel(model_function))

        @agent.tool_plain
        def structured_tool():
            return output

        run_result = await agent.run("go")
    finally:
        ToolManager.execute_tool_call = original_execute_tool_call
        ToolManager.get_tool_def = original_get_tool_def
        ToolManager.validate_tool_call = original_validate_tool_call
        ToolManager.validate_output_tool_call = original_validate_output_tool_call
        CallToolsNode._handle_tool_calls = original_handle_tool_calls
        callbacks.unregister_callback("pre_tool_call", add_context)

    assert run_result.output == "done"
    return seen["response"]


@pytest.mark.asyncio
async def test_finalizer_bounds_large_context_and_structured_output(_spill_root):
    full_context = "context-" + "c" * 5000
    full_output = "output-" + "o" * 50_000

    response = await _run_tool({"content": full_output}, full_context)

    envelope = json.loads(response)
    inline_bytes = sum(
        len(value.encode("utf-8"))
        for value in envelope.values()
        if isinstance(value, str)
    )
    assert "[hook context]" in response
    assert _NOTICE in response
    assert full_context not in response
    assert full_output not in response
    assert inline_bytes <= _CAP
    assert len(list(_spill_root.glob("session-*/*"))) == 2


@pytest.mark.asyncio
async def test_hook_envelope_can_spill_otherwise_unsupported_plain_string(
    _spill_root,
):
    full_output = "plain-" + "p" * 5000

    response = await _run_tool(full_output, "short policy context")

    assert _NOTICE in response
    assert full_output not in response
    assert len(list(_spill_root.glob("session-*/*"))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "forbidden", "expected"),
    [
        (_ExcludedOutput(secret="SECRET" * 1000), "SECRET", None),
        (_RedactedOutput(secret="SECRET" * 1000), "SECRET", "[REDACTED]"),
    ],
    ids=["excluded", "redacted"],
)
async def test_context_envelope_uses_pydantic_serialization_privacy(
    _spill_root,
    output,
    forbidden,
    expected,
):
    response = await _run_tool(output, "short policy context")

    assert forbidden not in response
    if expected is not None:
        assert expected in response
    assert not list(_spill_root.glob("session-*/*"))
