from unittest.mock import AsyncMock, patch

import pytest

from code_puppy.tools.agent_tools import AgentInvokeOutput
from code_puppy_core_plugins.auto_continue.agent import AutoContinueAgent
from code_puppy_core_plugins.auto_continue.register_callbacks import (
    _on_interactive_turn_end,
    _register_agents,
)


class _Result:
    output = "Want me to implement that next?"


def test_registers_tiny_toolless_agent():
    registration = _register_agents()[0]

    assert registration == {"name": "auto-continue", "class": AutoContinueAgent}
    assert AutoContinueAgent().get_available_tools() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", ["yes, go.", "continue", "okay"])
async def test_returns_approved_continuation(approval: str):
    invocation = AsyncMock(
        return_value=AgentInvokeOutput(
            response=approval,
            agent_name="auto-continue",
        )
    )
    with patch(
        "code_puppy.tools.subagent_invocation._invoke_agent_impl", invocation
    ):
        result = await _on_interactive_turn_end(object(), "do it", _Result())

    assert result == {"prompt": approval}
    assert invocation.await_args.kwargs["emit_response_message"] is False


@pytest.mark.asyncio
async def test_ignores_rejection_and_failed_runs():
    invocation = AsyncMock(
        return_value=AgentInvokeOutput(response="NO", agent_name="auto-continue")
    )
    with patch(
        "code_puppy.tools.subagent_invocation._invoke_agent_impl", invocation
    ):
        rejected = await _on_interactive_turn_end(object(), "do it", _Result())
        failed = await _on_interactive_turn_end(
            object(), "do it", _Result(), success=False
        )

    assert rejected is None
    assert failed is None
    invocation.assert_awaited_once()
