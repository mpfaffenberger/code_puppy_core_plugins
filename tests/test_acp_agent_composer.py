"""ACP agent composer plumbing: announce timing, ``_meta`` wiring, steer routing, release.

The shape and logic of advertised values belong to ``test_acp_composer_ext.py``;
here ``composer_ext`` is the oracle. New-core APIs are faked via ``sys.modules``;
the default is the old core. Real ``code_puppy`` modules are never imported.
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest
from acp.schema import CloseSessionResponse, PromptResponse

from code_puppy_core_plugins.acp import (
    agent as agent_mod,
    composer_ext,
    io_delegation,
    persistence,
    replay,
    session_config,
    state,
)
from code_puppy_core_plugins.acp.agent import CodePuppyAgent
from tests.acp_session_support import FakeAgent

CATALOG = "code_puppy.command_line.command_catalog"
REGISTRY = "code_puppy.command_line.command_registry"
PAUSE = "code_puppy.messaging.pause_controller"
SUBAGENTS = "code_puppy.tools.subagent_invocation"
MEDIA = "code_puppy.model_media"
CONFIG = "code_puppy.config"
BUILDER = "code_puppy.agents._builder"

MEDIA_KEY = "codePuppy/binaryMediaTypes"
STEER_METHOD = "code_puppy/session/steer"
SESSION_METHODS = ["new_session", "load_session", "resume_session", "set_config_option"]

MEDIA_TABLE = {"model-a": ["image/png", "image/jpeg"], "model-b": ["image/gif"]}


async def _noop_async(*args: Any, **kwargs: Any) -> None:
    return None


class FakeConnection:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []
        self.fail_next = False
        self.gate: asyncio.Event | None = None

    async def session_update(self, session_id: str, update: Any) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("transport closed")
        self.updates.append((session_id, update))


def _install(monkeypatch, name: str, **attrs: Any) -> None:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    monkeypatch.setitem(sys.modules, name, module)


def _entry(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=f"{name} command",
        hint="",
        interactive=False,
        aliases=[],
        category="core",
    )


def _announced(connection: FakeConnection, session_id: str) -> list[list[str]]:
    """Command names sent to ``session_id``, in send order."""
    return [
        [c.name for c in update.available_commands]
        for sid, update in connection.updates
        if sid == session_id
    ]


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _respond(agent: CodePuppyAgent, method: str, session_id: str) -> Any:
    """Call one of the four responses that carry composer ``_meta``."""
    if method == "new_session":
        return await agent.new_session(cwd="/w")
    if method == "set_config_option":
        return await agent.set_config_option("model", session_id, "model-b")
    return await getattr(agent, method)(cwd="/w", session_id=session_id)


# ---- Environment: old core by default -------------------------------------
@pytest.fixture(autouse=True)
def world(monkeypatch) -> SimpleNamespace:
    world = SimpleNamespace(model="model-a", fallback_scopes=[])

    def set_model(name: str) -> bool:
        world.model = name
        return True

    # Required: SetSessionConfigOptionResponse rejects an empty option list.
    monkeypatch.setattr(
        session_config, "config_options", lambda: [session_config._mode_option()]
    )
    monkeypatch.setattr(agent_mod, "asyncio", SimpleNamespace(sleep=_noop_async))
    monkeypatch.setattr(
        CodePuppyAgent,
        "_new_agent",
        staticmethod(lambda: FakeAgent(model_name=world.model)),
    )
    monkeypatch.setattr(persistence, "load_history", lambda session_id: None)
    monkeypatch.setattr(persistence, "delete", lambda session_id: None)
    monkeypatch.setattr(replay, "replay_history", _noop_async)
    monkeypatch.setattr(io_delegation, "install", lambda caps: None)
    monkeypatch.setattr(session_config, "set_model", set_model)
    _install(monkeypatch, CONFIG, get_global_model_name=lambda: world.model)
    _install(
        monkeypatch,
        BUILDER,
        reset_model_fallback_warnings=lambda scope=None: world.fallback_scopes.append(
            scope
        ),
    )
    # Empty legacy registry: blocks the real import on old-core paths.
    _install(monkeypatch, REGISTRY, get_unique_commands=lambda: [])
    for name in (CATALOG, PAUSE, SUBAGENTS, MEDIA):
        monkeypatch.setitem(sys.modules, name, None)
    return world


def install_catalog(monkeypatch) -> list[SimpleNamespace]:
    entries: list[SimpleNamespace] = []
    _install(monkeypatch, CATALOG, list_commands=lambda: list(entries))
    return entries


def install_pause(monkeypatch) -> SimpleNamespace:
    """Steer support plus the release hooks; returns the spies."""
    spies = SimpleNamespace(dropped=[], cleared=[])
    _install(
        monkeypatch,
        PAUSE,
        get_session_pause_controller=lambda session_id: None,
        drop_session_pause_controller=spies.dropped.append,
    )
    _install(monkeypatch, SUBAGENTS, clear_interrupted_subagents=spies.cleared.append)
    return spies


def install_media(monkeypatch) -> None:
    _install(
        monkeypatch,
        MEDIA,
        supported_binary_media_types=lambda model: MEDIA_TABLE.get(model, []),
    )


# ---- Fixtures -------------------------------------------------------------
@pytest.fixture
def agent() -> CodePuppyAgent:
    return CodePuppyAgent()


@pytest.fixture
async def connection():
    conn = FakeConnection()
    state.set_connection(conn, asyncio.get_running_loop())
    yield conn
    state.set_connection(None, None)


# ---- Available commands: announce plumbing -------------------------------
async def test_new_session_announces_the_catalog_once(agent, connection, monkeypatch):
    install_catalog(monkeypatch).append(_entry("/help"))

    response = await agent.new_session(cwd="/w")
    await _settle()

    assert len(connection.updates) == 1
    session_id, update = connection.updates[0]
    assert session_id == response.session_id
    assert update.session_update == "available_commands_update"
    assert update.available_commands == composer_ext.available_commands()


async def test_empty_catalog_sends_nothing(agent, connection, monkeypatch):
    install_catalog(monkeypatch)

    await agent.new_session(cwd="/w")
    await _settle()

    assert connection.updates == []


@pytest.mark.parametrize("trigger", ["set_config_option", "set_session_mode"])
async def test_unchanged_catalog_is_not_resent(agent, connection, monkeypatch, trigger):
    install_catalog(monkeypatch).append(_entry("/help"))
    session_id = (await agent.new_session(cwd="/w")).session_id
    await _settle()

    if trigger == "set_config_option":
        await agent.set_config_option("model", session_id, "model-b")
    else:
        await agent.set_session_mode("default", session_id)
    await _settle()

    assert len(connection.updates) == 1


async def test_changed_catalog_resends_to_every_open_session_only(
    agent, connection, monkeypatch
):
    entries = install_catalog(monkeypatch)
    entries.append(_entry("/help"))
    first = (await agent.new_session(cwd="/w")).session_id
    second = (await agent.new_session(cwd="/w")).session_id
    closed = (await agent.new_session(cwd="/w")).session_id
    await _settle()
    await agent.close_session(closed)

    entries.append(_entry("/clear"))
    await agent.set_config_option("model", first, "model-b")
    await _settle()

    for session_id in (first, second):
        assert _announced(connection, session_id) == [["help"], ["help", "clear"]]
    assert len(_announced(connection, closed)) == 1


async def test_failed_send_is_retried_on_next_refresh(agent, connection, monkeypatch):
    install_catalog(monkeypatch).append(_entry("/help"))
    connection.fail_next = True
    session_id = (await agent.new_session(cwd="/w")).session_id
    await _settle()
    assert agent._sessions[session_id].commands_sent is None

    await agent.set_config_option("model", session_id, "model-b")
    await _settle()

    assert _announced(connection, session_id) == [["help"]]


async def test_overlapping_announces_send_once(agent, connection, monkeypatch):
    install_catalog(monkeypatch).append(_entry("/help"))
    agent._make_session("sess_a", "/w", None, None)
    connection.gate = asyncio.Event()

    sends = [
        asyncio.ensure_future(agent._announce_commands("sess_a")) for _ in range(2)
    ]
    await _settle()
    connection.gate.set()
    await asyncio.gather(*sends)

    assert len(_announced(connection, "sess_a")) == 1


async def test_failed_awaited_send_restores_marker_for_next_announce(
    agent, connection, monkeypatch
):
    install_catalog(monkeypatch).append(_entry("/help"))
    session = agent._make_session("sess_a", "/w", None, None)
    connection.gate = asyncio.Event()
    connection.fail_next = True

    pending = asyncio.ensure_future(agent._announce_commands("sess_a"))
    await _settle()
    assert session.commands_sent is not None  # claimed while the send is in flight
    connection.gate.set()
    await pending
    assert session.commands_sent is None

    connection.gate = None
    await agent._announce_commands("sess_a")

    assert _announced(connection, "sess_a") == [["help"]]


@pytest.mark.parametrize("reopen", ["load_session", "resume_session"])
async def test_reopening_a_session_resends_its_commands(
    agent, connection, monkeypatch, reopen
):
    install_catalog(monkeypatch).append(_entry("/help"))
    session_id = (await agent.new_session(cwd="/w")).session_id
    await _settle()

    await getattr(agent, reopen)(cwd="/w", session_id=session_id)
    await _settle()

    assert _announced(connection, session_id) == [["help"], ["help"]]


# ---- Advertised _meta: each response delegates to composer_ext ------------
@pytest.mark.parametrize("new_core", [True, False], ids=["new-core-both", "old-core"])
async def test_initialize_advertises_composer_capabilities(
    agent, monkeypatch, new_core
):
    if new_core:
        install_pause(monkeypatch)
        install_media(monkeypatch)

    response = await agent.initialize(protocol_version=1)

    assert (response.agent_capabilities.field_meta is None) is (not new_core)
    assert response.agent_capabilities.field_meta == composer_ext.capabilities_meta()


@pytest.mark.parametrize("method", SESSION_METHODS)
@pytest.mark.parametrize("new_core", [True, False], ids=["new-core", "old-core"])
async def test_session_responses_advertise_session_meta(
    agent, monkeypatch, world, method, new_core
):
    if new_core:
        install_media(monkeypatch)
    session_id = (await agent.new_session(cwd="/w")).session_id
    world.model = "model-b"

    response = await _respond(agent, method, session_id)

    if not new_core:
        assert response.field_meta is None
        return
    target = response.session_id if method == "new_session" else session_id
    assert response.field_meta is not None
    assert response.field_meta == composer_ext.session_meta(agent._sessions[target])


async def test_model_switch_advertises_the_new_model_media(agent, monkeypatch):
    install_media(monkeypatch)
    session_id = (await agent.new_session(cwd="/w")).session_id

    response = await agent.set_config_option("model", session_id, "model-b")

    assert response.field_meta == {MEDIA_KEY: MEDIA_TABLE["model-b"]}


# ---- Steer ext routing ----------------------------------------------------
@pytest.mark.parametrize(
    ("session_id", "expected"),
    [
        ("sess_live", {"accepted": True}),
        ("sess_missing", {"accepted": False, "reason": "not_running"}),
    ],
    ids=["live-session", "unknown-session"],
)
async def test_steer_ext_method_routes_to_the_live_session(
    agent, monkeypatch, session_id, expected
):
    install_pause(monkeypatch)
    agent._sessions["sess_live"] = SimpleNamespace(
        steer=lambda blocks: {"accepted": True}
    )

    result = await agent.ext_method(
        STEER_METHOD, {"sessionId": session_id, "prompt": []}
    )

    assert result == expected


# ---- Session release and prompt meta --------------------------------------
@pytest.mark.parametrize("new_core", [True, False], ids=["new-core", "old-core"])
async def test_close_session_releases_pause_and_subagent_state(
    agent, monkeypatch, world, new_core
):
    spies = install_pause(monkeypatch) if new_core else None
    session_id = (await agent.new_session(cwd="/w")).session_id

    response = await agent.close_session(session_id)

    assert isinstance(response, CloseSessionResponse)
    assert session_id not in agent._sessions
    assert world.fallback_scopes[-1] == session_id
    if new_core:
        assert spies.dropped == [session_id]
        assert spies.cleared == [session_id]


@pytest.mark.parametrize("field_meta", [None, {"codePuppy/interrupted": []}])
async def test_prompt_response_carries_the_outcome_meta(agent, field_meta):
    async def prompt(blocks: list[Any]) -> SimpleNamespace:
        return SimpleNamespace(
            stop_reason="end_turn", usage=None, field_meta=field_meta
        )

    agent._sessions["sess_a"] = SimpleNamespace(prompt=prompt)

    response = await agent.prompt([], "sess_a")

    assert isinstance(response, PromptResponse)
    assert response.stop_reason == "end_turn"
    assert response.field_meta == field_meta


async def test_prompt_on_an_unknown_session_still_raises(agent):
    with pytest.raises(ValueError, match="unknown session: sess_missing"):
        await agent.prompt([], "sess_missing")
