"""ACP agent composer wiring: commands, ``_meta``, ext methods, session release.

New-core APIs are faked via ``sys.modules``; the default is the old core. Real
``code_puppy`` modules are never imported (slow, and circular in this venv).
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest
from acp import RequestError
from acp.schema import CloseSessionResponse, PromptResponse

from code_puppy_core_plugins.acp import (
    agent as agent_mod,
    io_delegation,
    persistence,
    replay,
    session_config,
    state,
)
from code_puppy_core_plugins.acp.agent import CodePuppyAgent

CATALOG = "code_puppy.command_line.command_catalog"
REGISTRY = "code_puppy.command_line.command_registry"
PAUSE = "code_puppy.messaging.pause_controller"
SUBAGENTS = "code_puppy.tools.subagent_invocation"
MEDIA = "code_puppy.model_media"
CONFIG = "code_puppy.config"
BUILDER = "code_puppy.agents._builder"

STEER_KEY = "codePuppy/steer"
MEDIA_KEY = "codePuppy/binaryMediaTypes"
COMMAND_KEY = "codePuppy/command"
STEER_METHOD = "code_puppy/session/steer"
SESSION_METHODS = ["new_session", "load_session", "resume_session", "set_config_option"]

IMAGE_TYPES = ["image/png", "image/jpeg"]
MEDIA_TABLE = {"model-a": IMAGE_TYPES, "model-b": ["image/gif"]}


async def _noop_async(*args: Any, **kwargs: Any) -> None:
    return None


class FakeAgent:
    def __init__(self, model: str) -> None:
        self.model = model
        self.history: list[Any] = []

    def get_model_name(self) -> str:
        return self.model

    def get_message_history(self) -> list[Any]:
        return list(self.history)

    def set_message_history(self, history: list[Any]) -> None:
        self.history = list(history)


class FakeConnection:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []
        self.fail_next = False

    async def session_update(self, session_id: str, update: Any) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("transport closed")
        self.updates.append((session_id, update))


def _module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def _entry(name: str, description: str, **fields: Any) -> SimpleNamespace:
    defaults = {"hint": "", "interactive": False, "aliases": [], "category": "core"}
    return SimpleNamespace(name=name, description=description, **{**defaults, **fields})


def _wire(
    name: str,
    description: str,
    hint: str | None = None,
    interactive: bool = False,
    aliases: tuple[str, ...] = (),
    category: str = "core",
) -> dict[str, Any]:
    """The exact ``available_commands`` entry the client receives."""
    meta = {"interactive": interactive, "aliases": list(aliases), "category": category}
    wire = {"name": name, "description": description, "_meta": {COMMAND_KEY: meta}}
    return {**wire, "input": {"hint": hint}} if hint is not None else wire


def _announced(connection: FakeConnection, session_id: str) -> list[list[dict]]:
    """Command lists sent to ``session_id`` as wire dicts, in send order."""
    return [
        [
            c.model_dump(by_alias=True, exclude_none=True, mode="json")
            for c in u.available_commands
        ]
        for sid, u in connection.updates
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
    world = SimpleNamespace(model="model-a", legacy_commands=[], fallback_scopes=[])

    def set_model(name: str) -> bool:
        world.model = name
        return True

    # Required: SetSessionConfigOptionResponse rejects an empty option list.
    monkeypatch.setattr(
        session_config, "config_options", lambda: [session_config._mode_option()]
    )
    monkeypatch.setattr(agent_mod, "asyncio", SimpleNamespace(sleep=_noop_async))
    monkeypatch.setattr(
        CodePuppyAgent, "_new_agent", staticmethod(lambda: FakeAgent(world.model))
    )
    monkeypatch.setattr(persistence, "load_history", lambda session_id: None)
    monkeypatch.setattr(persistence, "delete", lambda session_id: None)
    monkeypatch.setattr(replay, "replay_history", _noop_async)
    monkeypatch.setattr(io_delegation, "install", lambda caps: None)
    monkeypatch.setattr(session_config, "set_model", set_model)
    monkeypatch.setitem(
        sys.modules, CONFIG, _module(CONFIG, get_global_model_name=lambda: world.model)
    )
    monkeypatch.setitem(
        sys.modules,
        BUILDER,
        _module(
            BUILDER,
            reset_model_fallback_warnings=lambda scope=None: (
                world.fallback_scopes.append(scope)
            ),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        REGISTRY,
        _module(REGISTRY, get_unique_commands=lambda: list(world.legacy_commands)),
    )
    for name in (CATALOG, PAUSE, SUBAGENTS, MEDIA):
        monkeypatch.setitem(sys.modules, name, None)
    return world


def install_catalog(monkeypatch) -> list[SimpleNamespace]:
    entries: list[SimpleNamespace] = []
    monkeypatch.setitem(
        sys.modules, CATALOG, _module(CATALOG, list_commands=lambda: list(entries))
    )
    return entries


def install_pause(monkeypatch) -> SimpleNamespace:
    """Steer support plus the release hooks; returns the spies."""
    spies = SimpleNamespace(dropped=[], cleared=[])
    monkeypatch.setitem(
        sys.modules,
        PAUSE,
        _module(
            PAUSE,
            get_session_pause_controller=lambda session_id: None,
            drop_session_pause_controller=spies.dropped.append,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        SUBAGENTS,
        _module(SUBAGENTS, clear_interrupted_subagents=spies.cleared.append),
    )
    return spies


def install_media(monkeypatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        MEDIA,
        _module(MEDIA, supported_binary_media_types=lambda m: MEDIA_TABLE.get(m, [])),
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


# ---- Available commands ---------------------------------------------------
async def test_new_session_announces_exact_command_wire_shape(
    agent, connection, monkeypatch
):
    entries = install_catalog(monkeypatch)
    entries.append(
        _entry("/model", "Pick a model", hint="<name>", interactive=True, aliases=["m"])
    )
    entries.append(_entry("/clear", "Clear history", category="session"))

    response = await agent.new_session(cwd="/w")
    await _settle()

    assert connection.updates[0][1].session_update == "available_commands_update"
    assert _announced(connection, response.session_id) == [
        [
            _wire(
                "model", "Pick a model", hint="<name>", interactive=True, aliases=["m"]
            ),
            _wire("clear", "Clear history", category="session"),
        ]
    ]


async def test_old_core_falls_back_to_registry_with_default_meta(
    agent, connection, world
):
    world.legacy_commands = [SimpleNamespace(name="help", description="Show help")]

    response = await agent.new_session(cwd="/w")
    await _settle()

    assert _announced(connection, response.session_id) == [[_wire("help", "Show help")]]


async def test_no_commands_sends_nothing(agent, connection, monkeypatch):
    install_catalog(monkeypatch)

    await agent.new_session(cwd="/w")
    await _settle()

    assert connection.updates == []


@pytest.mark.parametrize("trigger", ["set_config_option", "set_session_mode"])
async def test_unchanged_catalog_is_not_resent(agent, connection, monkeypatch, trigger):
    install_catalog(monkeypatch).append(_entry("/help", "Help"))
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
    entries.append(_entry("/help", "Help"))
    first = (await agent.new_session(cwd="/w")).session_id
    second = (await agent.new_session(cwd="/w")).session_id
    closed = (await agent.new_session(cwd="/w")).session_id
    await _settle()
    await agent.close_session(closed)

    entries.append(_entry("/clear", "Clear history"))
    await agent.set_session_mode("default", first)
    await _settle()

    expected = [_wire("help", "Help"), _wire("clear", "Clear history")]
    for session_id in (first, second):
        assert _announced(connection, session_id)[-1] == expected
        assert len(_announced(connection, session_id)) == 2
    assert len(_announced(connection, closed)) == 1


async def test_failed_send_is_retried_on_next_refresh(agent, connection, monkeypatch):
    install_catalog(monkeypatch).append(_entry("/help", "Help"))
    connection.fail_next = True
    session_id = (await agent.new_session(cwd="/w")).session_id
    await _settle()
    assert agent._sessions[session_id].commands_sent is None

    await agent.set_session_mode("default", session_id)
    await _settle()

    assert len(_announced(connection, session_id)) == 1


@pytest.mark.parametrize("reopen", ["load_session", "resume_session"])
async def test_reopening_a_session_resends_its_commands(
    agent, connection, monkeypatch, reopen
):
    install_catalog(monkeypatch).append(_entry("/help", "Help"))
    session_id = (await agent.new_session(cwd="/w")).session_id
    await _settle()

    await getattr(agent, reopen)(cwd="/w", session_id=session_id)
    await _settle()

    assert _announced(connection, session_id) == [[_wire("help", "Help")]] * 2


# ---- Advertised _meta -----------------------------------------------------
@pytest.mark.parametrize(
    ("steer", "media", "expected"),
    [
        (True, True, {STEER_KEY: True, MEDIA_KEY: IMAGE_TYPES}),
        (True, False, {STEER_KEY: True}),
        (False, True, {MEDIA_KEY: IMAGE_TYPES}),
        (False, False, None),
    ],
    ids=["steer-and-media", "steer-only", "media-only", "old-core"],
)
async def test_initialize_advertises_only_available_composer_meta(
    agent, monkeypatch, steer, media, expected
):
    if steer:
        install_pause(monkeypatch)
    if media:
        install_media(monkeypatch)

    response = await agent.initialize(protocol_version=1)

    assert response.agent_capabilities.field_meta == expected


@pytest.mark.parametrize("method", SESSION_METHODS)
@pytest.mark.parametrize("new_core", [True, False], ids=["new-core", "old-core"])
async def test_session_responses_advertise_the_session_model_media(
    agent, monkeypatch, world, method, new_core
):
    if new_core:
        install_media(monkeypatch)
    session_id = (await agent.new_session(cwd="/w")).session_id
    world.model = "model-b"

    response = await _respond(agent, method, session_id)

    expected = {MEDIA_KEY: MEDIA_TABLE["model-b"]} if new_core else None
    assert response.field_meta == expected


# ---- ext_method -----------------------------------------------------------
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


@pytest.mark.parametrize(
    ("method", "new_core"),
    [("code_puppy/unknown", True), (STEER_METHOD, False)],
    ids=["unknown-method", "steer-on-old-core"],
)
async def test_unroutable_ext_methods_are_method_not_found(
    agent, monkeypatch, method, new_core
):
    if new_core:
        install_pause(monkeypatch)

    with pytest.raises(RequestError) as raised:
        await agent.ext_method(method, {"sessionId": "sess_a", "prompt": []})

    assert raised.value.code == -32601  # JSON-RPC method not found


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
