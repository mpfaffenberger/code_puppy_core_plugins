"""Capture routing is explicit and does not opt tools into speculation."""

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from code_puppy_core_plugins.computer_use import tools


@pytest.mark.parametrize(
    "register,name",
    [
        (tools.register_get_app_state, "computer_get_app_state"),
        (tools.register_snapshot, "computer_snapshot"),
        (tools.register_screenshot, "computer_screenshot"),
        (tools.register_batch, "computer_use_batch"),
    ],
)
def test_capture_tools_declare_native_routing(register, name):
    agent = Agent(TestModel())
    register(agent)
    metadata = agent._function_toolset.tools[name].metadata
    assert metadata == {"code_mode_native": True}


def test_mutation_only_tool_remains_sandbox_eligible():
    agent = Agent(TestModel())
    tools.register_click(agent)
    assert not (agent._function_toolset.tools["computer_click"].metadata or {}).get(
        "code_mode_native"
    )
