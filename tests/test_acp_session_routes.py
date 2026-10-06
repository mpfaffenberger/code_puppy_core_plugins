"""ACP session routes: each session owns the agent and model it runs on.

A route change is session-local (the terminal's global model is never
written), is reported only once it is built and saved, and survives a
restart. Every session response carries the route under
``_meta.codePuppyRoute`` with an epoch that advances on a real change.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio
from acp.schema import TextContentBlock
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from code_puppy_core_plugins.acp import io_delegation, permissions, route_runtime, state
from code_puppy_core_plugins.acp.agent import CodePuppyAgent
from code_puppy_core_plugins.acp.route import (
    CAPABILITY_META_KEY,
    META_KEY,
    RouteUnavailable,
)

MODELS = ["model-a", "model-b"]
AGENTS = {"code-puppy": "model-a", "planner": "model-b"}
HISTORY = [
    ModelRequest(parts=[UserPromptPart(content="hello")]),
    ModelResponse(parts=[TextPart(content="hi there")]),
]


class _Agent:
    """Enough of ``BaseAgent`` for routes: a default model, an exact build."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._message_history: list[Any] = []
        self._override: str | None = None
        self._last_model_name: str | None = None
        self.mcp_toolsets: list[Any] = []

    def get_model_name(self) -> str:
        return self._override or AGENTS[self.name]

    def set_runtime_model_name_override(self, model, *, allow_fallback=None):
        assert allow_fallback is False
        self._override = model

    def reload_code_generation_agent(self) -> None:
        self._last_model_name = self.get_model_name()

    def get_message_history(self) -> list[Any]:
        return self._message_history

    def set_message_history(self, history: list[Any]) -> None:
        self._message_history = list(history)

    def estimate_tokens_for_message(self, _message: Any) -> int:
        return 1

    def set_runtime_mcp_toolsets(self, toolsets: list[Any]) -> None:
        self.mcp_toolsets = list(toolsets)


class _Connection:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))


def _load_agent(name: str, *, allow_fallback: bool = True) -> _Agent:
    if name not in AGENTS:
        raise ValueError(f"Agent '{name}' not found")
    return _Agent(name)


@pytest.fixture(autouse=True)
def _routes(monkeypatch):
    global_writes: list[str] = []
    monkeypatch.setattr(
        "code_puppy.agents.agent_manager.get_current_agent_name", lambda: "code-puppy"
    )
    monkeypatch.setattr("code_puppy.agents.agent_manager.load_agent", _load_agent)
    monkeypatch.setattr(
        "code_puppy.agents.agent_manager.get_available_agents",
        lambda: {name: name for name in AGENTS},
    )
    monkeypatch.setattr(
        "code_puppy.command_line.model_picker_completion.load_model_names",
        lambda: list(MODELS),
    )
    monkeypatch.setattr("code_puppy.config.set_model_name", global_writes.append)
    return global_writes


@pytest_asyncio.fixture
async def connected(tmp_path):
    def _connect(**kwargs: Any) -> tuple[CodePuppyAgent, _Connection]:
        conn = _Connection()
        agent = CodePuppyAgent(persistence_base_dir=tmp_path, **kwargs)
        agent.on_connect(conn)
        return agent, conn

    yield _connect
    permissions.uninstall()
    io_delegation.uninstall()
    state.set_connection(None, None)


def _route(response: Any) -> dict[str, Any]:
    return response.field_meta[META_KEY]


def _prompt(text: str) -> list[Any]:
    return [TextContentBlock(type="text", text=text)]


@pytest.mark.asyncio
async def test_initialize_advertises_the_route_contract(connected):
    agent, _ = connected()

    response = await agent.initialize(protocol_version=1)

    contract = response.agent_info.field_meta[CAPABILITY_META_KEY]
    assert contract["version"] == 1
    assert contract["routeMetaKey"] == META_KEY
    assert contract["configOptionIds"] == {"agent": "agent", "model": "model"}


@pytest.mark.asyncio
async def test_new_session_reports_the_route_it_built(connected):
    agent, _ = connected()

    response = await agent.new_session(cwd="/tmp")

    route = _route(response)
    assert route == {
        "version": 1,
        "sessionId": response.session_id,
        "agentId": "code-puppy",
        "modelId": "model-a",
        "routeEpoch": 1,
    }
    ids = {option.id: option.current_value for option in response.config_options}
    assert ids["model"] == "model-a"
    assert ids["agent"] == "code-puppy"


@pytest.mark.asyncio
async def test_model_switch_is_session_local_and_advances_the_epoch(connected, _routes):
    agent, conn = connected()
    first = await agent.new_session(cwd="/tmp")
    second = await agent.new_session(cwd="/tmp")

    response = await agent.set_config_option("model", first.session_id, "model-b")

    assert _route(response)["modelId"] == "model-b"
    assert _route(response)["routeEpoch"] == 2
    assert agent._sessions[first.session_id].agent.get_model_name() == "model-b"
    assert agent._sessions[second.session_id].route.model_id == "model-a"
    assert _routes == []  # the global model was never written
    [update] = [
        u for _, u in conn.updates if u.session_update == "config_option_update"
    ]
    assert update.field_meta[META_KEY]["routeEpoch"] == 2


@pytest.mark.asyncio
async def test_reselecting_the_current_model_keeps_the_epoch(connected):
    agent, conn = connected()
    session = await agent.new_session(cwd="/tmp")

    response = await agent.set_config_option("model", session.session_id, "model-a")

    assert _route(response)["routeEpoch"] == 1
    assert not [
        u for _, u in conn.updates if u.session_update == "config_option_update"
    ]


@pytest.mark.asyncio
async def test_unknown_model_is_rejected_and_the_route_stands(connected):
    agent, _ = connected()
    session = await agent.new_session(cwd="/tmp")
    live = agent._sessions[session.session_id]
    before = (live.agent, live.route)

    with pytest.raises(ValueError, match="not configured"):
        await agent.set_config_option("model", session.session_id, "model-z")

    assert (live.agent, live.route) == before


@pytest.mark.asyncio
async def test_agent_switch_uses_the_new_agents_model(connected):
    agent, _ = connected()
    session = await agent.new_session(cwd="/tmp")
    agent._sessions[session.session_id].agent.set_message_history(["keep"])

    response = await agent.set_config_option("agent", session.session_id, "planner")

    route = _route(response)
    assert (route["agentId"], route["modelId"], route["routeEpoch"]) == (
        "planner",
        "model-a",
        2,
    )
    assert agent._sessions[session.session_id].agent.get_message_history() == ["keep"]


@pytest.mark.asyncio
async def test_stale_expected_epoch_is_refused(connected):
    agent, _ = connected()
    session = await agent.new_session(cwd="/tmp")
    await agent.set_config_option("model", session.session_id, "model-b")

    with pytest.raises(ValueError, match="stale route epoch 1"):
        await agent.set_config_option(
            "model",
            session.session_id,
            "model-a",
            field_meta={META_KEY: {"expectedRouteEpoch": 1}},
        )

    assert agent._sessions[session.session_id].route.model_id == "model-b"


@pytest.mark.asyncio
async def test_a_switched_route_survives_a_restart(connected):
    agent, _ = connected()
    session = await agent.new_session(cwd="/work")
    agent._sessions[session.session_id].agent.set_message_history(HISTORY)
    await agent.set_config_option("model", session.session_id, "model-b")

    restarted, _ = connected()
    response = await restarted.load_session(cwd="/work", session_id=session.session_id)

    assert _route(response)["modelId"] == "model-b"
    assert _route(response)["routeEpoch"] == 2
    live = restarted._sessions[session.session_id]
    assert live.agent.get_model_name() == "model-b"
    assert len(live.agent.get_message_history()) == 2


@pytest.mark.asyncio
async def test_a_session_saved_before_routes_opens_on_the_default_route(
    connected, tmp_path
):
    (tmp_path / "legacy_acp.json").write_text(
        json.dumps({"session_id": "legacy", "cwd": "/work"}), encoding="utf-8"
    )
    agent, _ = connected()

    response = await agent.load_session(cwd="/work", session_id="legacy")

    assert _route(response)["modelId"] == "model-a"
    assert _route(response)["routeEpoch"] == 1


@pytest.mark.asyncio
async def test_an_unreadable_stored_route_refuses_to_load(connected, tmp_path):
    (tmp_path / "broken_acp.json").write_text(
        json.dumps({"session_id": "broken", "route": {"version": 99}}),
        encoding="utf-8",
    )
    agent, _ = connected()

    with pytest.raises(RouteUnavailable):
        await agent.load_session(cwd="/work", session_id="broken")


@pytest.mark.asyncio
async def test_fork_starts_on_its_sources_route_at_epoch_one(connected):
    agent, _ = connected()
    source = await agent.new_session(cwd="/tmp")
    await agent.set_config_option("model", source.session_id, "model-b")

    fork = await agent.fork_session(cwd="/tmp", session_id=source.session_id)

    route = _route(fork)
    assert route["sessionId"] == fork.session_id
    assert (route["modelId"], route["routeEpoch"]) == ("model-b", 1)


@pytest.mark.asyncio
async def test_model_command_switches_this_session(connected, _routes):
    agent, conn = connected()
    session = await agent.new_session(cwd="/tmp")

    response = await agent.prompt(_prompt("/model model-b"), session.session_id)

    assert response.stop_reason == "end_turn"
    assert agent._sessions[session.session_id].route.model_id == "model-b"
    assert _routes == []
    texts = [
        u.content.text
        for _, u in conn.updates
        if u.session_update == "agent_message_chunk"
    ]
    assert texts == ["Switched this session's model to model-b."]


def test_bare_route_commands_are_left_to_the_command_handler():
    from code_puppy_core_plugins.acp.route_changes import route_command

    assert route_command(_prompt("/model")) is None
    assert route_command(_prompt("/agent planner extra")) is None
    assert route_command(_prompt("tell me about /model x")) is None
    assert route_command(_prompt("/m model-b")) == ("model", "model-b")


def test_startup_flags_are_checked_before_serving():
    with pytest.raises(ValueError, match="not configured"):
        CodePuppyAgent(default_model_id="model-z")
    with pytest.raises(ValueError, match="not found"):
        CodePuppyAgent(default_agent_name="nobody")


def test_build_refuses_a_model_the_agent_did_not_build(monkeypatch):
    # The core quietly built another model (e.g. an older core that fell back).
    monkeypatch.setattr(
        _Agent,
        "reload_code_generation_agent",
        lambda self: setattr(self, "_last_model_name", "model-a"),
    )

    with pytest.raises(ValueError, match="expected exactly 'model-b'"):
        route_runtime.build_agent("code-puppy", "model-b", epoch=1)


@pytest.mark.asyncio
async def test_loading_a_live_session_reuses_it(connected):
    agent, _ = connected()
    session = await agent.new_session(cwd="/tmp")
    await agent.set_config_option("model", session.session_id, "model-b")
    live = agent._sessions[session.session_id]

    response = await agent.load_session(cwd="/tmp", session_id=session.session_id)

    assert agent._sessions[session.session_id] is live
    assert _route(response)["modelId"] == "model-b"
    assert _route(response)["routeEpoch"] == 2
