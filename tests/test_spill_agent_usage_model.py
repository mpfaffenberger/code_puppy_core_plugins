"""Spill coverage for agent results carrying nested usage metadata."""

from __future__ import annotations

import pytest

from code_puppy import config
from code_puppy.tools import agent_tools

from code_puppy_core_plugins.spill import register_callbacks as spill
from code_puppy_core_plugins.spill.result_shapes import model_facing_mapping

AgentInvokeWithModelOutput = getattr(agent_tools, "AgentInvokeWithModelOutput", None)
SubagentRequestUsage = getattr(agent_tools, "SubagentRequestUsage", None)
pytestmark = pytest.mark.skipif(
    AgentInvokeWithModelOutput is None or SubagentRequestUsage is None,
    reason="runtime predates per-request subagent usage",
)


@pytest.fixture(autouse=True)
def _spill_root(tmp_path):
    root = tmp_path / "spills"
    config.set_value(spill.ROOT_KEY, str(root))
    config.set_value(spill.MAX_INLINE_KEY, "700")
    config.set_value(spill.PREVIEW_KEY, "100")
    spill._reset_state()
    yield root
    spill._reset_state()


def test_agent_result_spills_response_without_touching_nested_usage(_spill_root):
    usage = SubagentRequestUsage(
        model_name="test-model",
        input_tokens=10,
        cache_read_input_tokens=2,
        output_tokens=3,
    )
    result = AgentInvokeWithModelOutput(
        response="agent-head\n" + "x" * 5000 + "\nagent-tail",
        agent_name="researcher",
        per_request_usage=[usage],
        input_tokens=10,
        output_tokens=3,
        num_requests=1,
    )

    spill._spill_result("invoke_agent_with_model", result, "agent-session")

    files = list(_spill_root.glob("session-*/*"))
    assert len(files) == 1
    assert "Full output stored at:" in result.response
    assert result.per_request_usage == [usage]
    assert result.per_request_usage[0] is usage


def test_agent_result_rejects_hostile_nested_usage_storage_without_iteration():
    calls = []

    class HostileList(list):
        def __iter__(self):
            calls.append("iter")
            raise AssertionError("hostile list iteration executed")

    usage = SubagentRequestUsage(model_name="test-model", input_tokens=1)
    result = AgentInvokeWithModelOutput(
        response="response",
        agent_name="researcher",
        per_request_usage=[usage],
    )
    raw_values = dict(object.__getattribute__(result, "__dict__"))
    raw_values["per_request_usage"] = HostileList([usage])
    object.__setattr__(result, "__dict__", raw_values)

    assert model_facing_mapping(result) is None
    assert calls == []


def test_agent_result_rejects_corrupt_nested_model_private_state():
    usage = SubagentRequestUsage(model_name="test-model", input_tokens=1)
    object.__setattr__(usage, "__pydantic_private__", {})
    result = AgentInvokeWithModelOutput(
        response="response",
        agent_name="researcher",
        per_request_usage=[usage],
    )

    assert model_facing_mapping(result) is None
