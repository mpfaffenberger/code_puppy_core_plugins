"""Wire-level contract for the reply-feedback capability in agentCapabilities._meta.

Covers capabilities.py :: agent_capabilities() and agent.py :: initialize() as
serialized by the SDK, so assertions see exactly what an ACP client receives.
"""

from __future__ import annotations

from typing import Any

import pytest

from code_puppy_core_plugins.acp import io_delegation, permissions
from code_puppy_core_plugins.acp.agent import CodePuppyAgent
from code_puppy_core_plugins.acp.capabilities import (
    FEEDBACK_METHOD,
    FEEDBACK_VERSION,
    agent_capabilities,
)

FEEDBACK_META = {
    "codePuppy": {"feedback": {"method": "_code_puppy/feedback", "version": 1}}
}


def _wire(model: Any) -> dict[str, Any]:
    return model.model_dump(
        mode="json", by_alias=True, exclude_none=True, exclude_unset=True
    )


@pytest.fixture
def agent():
    instance = CodePuppyAgent()
    yield instance
    io_delegation.uninstall()
    permissions.uninstall()


def test_feedback_constants_match_advertised_contract():
    assert FEEDBACK_METHOD == "_code_puppy/feedback"
    assert FEEDBACK_VERSION == 1


def test_capabilities_wire_advertises_feedback_and_keeps_existing_keys():
    wire = _wire(agent_capabilities())

    assert wire["_meta"] == FEEDBACK_META
    assert wire["loadSession"] is True
    assert wire["promptCapabilities"] == {
        "image": True,
        "audio": False,
        "embeddedContext": True,
    }
    assert set(wire["sessionCapabilities"]) == {
        "list",
        "close",
        "fork",
        "resume",
        "additionalDirectories",
    }


def test_feedback_meta_is_fresh_per_call():
    first = agent_capabilities()
    first.field_meta["codePuppy"]["feedback"]["version"] = 99
    first.field_meta["injected"] = True

    second = agent_capabilities()

    assert second.field_meta == FEEDBACK_META


async def test_initialize_response_carries_feedback_meta(agent):
    wire_init = _wire(await agent.initialize(protocol_version=1))

    assert wire_init["agentCapabilities"]["_meta"] == FEEDBACK_META
