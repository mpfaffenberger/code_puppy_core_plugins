"""ACP session lifecycle: advertised methods are routed, resume does not replay.

The SDK registers ``session/close``, ``session/fork`` and ``session/resume``
as unstable methods and answers "method not found" for them unless the
connection opts in. Code Puppy advertises all three, so ``_serve`` must opt
in. ``session/resume`` restores a thread the client is still showing, so it
must rehydrate history without streaming it back the way ``session/load``
does.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from acp.agent.router import build_agent_router
from acp.meta import AGENT_METHODS
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from code_puppy_core_plugins.acp import (
    capabilities,
    io_delegation,
    permissions,
    register_callbacks,
    state,
)
from code_puppy_core_plugins.acp.agent import CodePuppyAgent

HISTORY = [
    ModelRequest(parts=[UserPromptPart(content="hello")]),
    ModelResponse(parts=[TextPart(content="hi there")]),
]


class _Connection:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))


class _Agent:
    def __init__(self) -> None:
        self._message_history: list[Any] = []
        self._last_model_name = "test-model"

    def get_model_name(self) -> str:
        return "test-model"

    def set_runtime_model_name_override(self, model_name: str, **_: Any) -> None:
        pass

    def reload_code_generation_agent(self) -> None:
        pass

    def get_message_history(self) -> list[Any]:
        return self._message_history

    def set_message_history(self, history: list[Any]) -> None:
        self._message_history = list(history)


@pytest_asyncio.fixture
async def wired_agent(monkeypatch):
    monkeypatch.setattr(
        "code_puppy.agents.agent_manager.get_current_agent_name", lambda: "code-puppy"
    )
    monkeypatch.setattr(
        "code_puppy.agents.agent_manager.load_agent", lambda name, **_: _Agent()
    )
    monkeypatch.setattr(
        "code_puppy.command_line.model_picker_completion.load_model_names",
        lambda: ["test-model"],
    )
    monkeypatch.setattr(
        "code_puppy_core_plugins.acp.persistence.load_history", lambda sid: HISTORY
    )
    conn = _Connection()
    agent = CodePuppyAgent()
    agent.on_connect(conn)
    yield agent, conn
    permissions.uninstall()
    io_delegation.uninstall()
    state.set_connection(None, None)


def _replayed_kinds(conn: _Connection) -> list[str]:
    return [getattr(update, "session_update", None) for _, update in conn.updates]


async def _serve_kwargs(monkeypatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_run_agent(agent: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("acp.run_agent", fake_run_agent)
    # _serve points the global console and root logger at stderr; keep this
    # test from changing either for the rest of the session.
    monkeypatch.setattr(
        "code_puppy.agents.event_stream_handler.set_streaming_console",
        lambda console: None,
    )
    monkeypatch.setattr("logging.basicConfig", lambda **kwargs: None)
    await register_callbacks._serve()
    return captured


@pytest.mark.asyncio
async def test_serve_opts_in_to_the_unstable_session_methods(monkeypatch):
    kwargs = await _serve_kwargs(monkeypatch)

    assert kwargs.get("use_unstable_protocol") is True


@pytest.mark.asyncio
async def test_every_advertised_session_capability_is_routed(monkeypatch):
    """Advertising a session capability the router refuses is a broken promise."""
    kwargs = await _serve_kwargs(monkeypatch)
    router = build_agent_router(CodePuppyAgent(), **kwargs)
    advertised = capabilities.agent_capabilities().session_capabilities
    methods = {
        "close": AGENT_METHODS["session_close"],
        "fork": AGENT_METHODS["session_fork"],
        "list": AGENT_METHODS["session_list"],
        "resume": AGENT_METHODS["session_resume"],
    }

    for capability, method in methods.items():
        assert getattr(advertised, capability) is not None
        route = router._requests[method]
        assert route.func is not None, method
        assert not route.warn_unstable, f"{method} is advertised but not routed"


@pytest.mark.asyncio
async def test_resume_rehydrates_history_without_replaying_it(wired_agent):
    agent, conn = wired_agent

    await agent.resume_session(cwd="/tmp", session_id="sess_resumed")

    assert agent._sessions["sess_resumed"].agent.get_message_history() == HISTORY
    replayed = {"user_message_chunk", "agent_message_chunk"}
    assert replayed.isdisjoint(_replayed_kinds(conn))


@pytest.mark.asyncio
async def test_load_still_replays_history(wired_agent):
    agent, conn = wired_agent

    await agent.load_session(cwd="/tmp", session_id="sess_loaded")

    kinds = _replayed_kinds(conn)
    assert agent._sessions["sess_loaded"].agent.get_message_history() == HISTORY
    assert kinds.index("user_message_chunk") < kinds.index("agent_message_chunk")
