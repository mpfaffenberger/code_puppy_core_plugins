"""Plugin notices reach ACP clients as finished ``tool_call`` rows, via the real core trigger.

The path under test is ``callbacks.on_banner_notice`` (sync trigger) ->
``EventBridge._on_banner_notice`` -> ``_send``. ``notice`` builds the wire banner.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from code_puppy import callbacks
from code_puppy.banners import notice

from code_puppy_core_plugins.acp import capabilities, io_delegation, permissions, state
from code_puppy_core_plugins.acp.agent import CodePuppyAgent
from code_puppy_core_plugins.acp.bridge import EventBridge

TOOL_CALL_ID = re.compile(r"^notice-[0-9a-f]{32}$")
BANNER_CAPABILITY = {"code_puppy": {"banners": {"v": 1}}}


class _Connection:
    def __init__(self, error: Exception | None = None) -> None:
        self.updates: list[tuple[str, Any]] = []
        self._error = error

    async def session_update(self, session_id: str, update: Any) -> None:
        if self._error is not None:
            raise self._error
        self.updates.append((session_id, update))


def _wire(update: Any) -> dict[str, Any]:
    return update.model_dump(by_alias=True, exclude_none=True)


async def _flush(bridge: EventBridge) -> None:
    # Snapshot now: finished tasks remove themselves from the set via done-callbacks.
    await asyncio.gather(*list(bridge._notice_tasks))


@pytest.fixture
def connection():
    conn = _Connection()
    state.set_connection(conn, None)
    yield conn
    state.end_run()
    state.set_connection(None, None)


@pytest.fixture
def run(connection):
    state.begin_run("s1")
    return connection


@pytest.fixture
def bridge():
    registered = EventBridge()
    registered.register()
    yield registered
    registered.unregister()


@pytest.mark.asyncio
async def test_notice_is_delivered_as_finished_tool_call(run, bridge):
    banner = notice("kb.card", "KB", details="card x (2)", target="card-x")

    callbacks.on_banner_notice(banner.to_wire())
    await _flush(bridge)

    [(session_id, update)] = run.updates
    wire = _wire(update)
    assert session_id == "s1"
    assert wire["sessionUpdate"] == "tool_call"
    assert TOOL_CALL_ID.match(wire["toolCallId"])
    assert wire["kind"] == "other"
    assert wire["status"] == "completed"
    assert wire["title"] == banner.title
    assert wire["_meta"] == {"code_puppy": {"banner": banner.to_wire()}}
    assert wire["_meta"]["code_puppy"]["banner"]["event"] == "plugin_notice"
    assert wire["_meta"]["code_puppy"]["banner"]["status"] == "info"


@pytest.mark.asyncio
async def test_each_notice_gets_a_distinct_tool_call_id(run, bridge):
    for _ in range(2):
        callbacks.on_banner_notice(notice("kb.card", "KB").to_wire())
    await _flush(bridge)

    ids = [_wire(update)["toolCallId"] for _, update in run.updates]
    assert len(ids) == 2
    assert ids[0] != ids[1]


@pytest.mark.asyncio
async def test_no_active_session_drops_notice(connection, bridge):
    callbacks.on_banner_notice(notice("kb.card", "KB").to_wire())
    await _flush(bridge)

    assert connection.updates == []


def test_notice_without_running_loop_is_dropped(run):
    bridge = EventBridge()

    bridge._on_banner_notice(notice("kb.card", "KB").to_wire())

    assert run.updates == []
    assert bridge._notice_tasks == set()


@pytest.mark.asyncio
async def test_failing_connection_is_swallowed_and_logged(run, bridge, caplog):
    state.set_connection(_Connection(error=RuntimeError("transport down")), None)

    callbacks.on_banner_notice(notice("kb.card", "KB").to_wire())
    # gather re-raises any task exception, so reaching the assert proves none escaped.
    await _flush(bridge)

    assert "Failed to emit session/update" in caplog.text


@pytest.mark.asyncio
async def test_register_and_unregister_round_trip_leaves_registry_as_found(run):
    bridge = EventBridge()
    before = callbacks.get_callbacks("banner_notice")

    bridge.register()
    assert bridge._on_banner_notice in callbacks.get_callbacks("banner_notice")
    bridge.unregister()

    assert bridge._on_banner_notice not in callbacks.get_callbacks("banner_notice")
    assert callbacks.get_callbacks("banner_notice") == before

    callbacks.on_banner_notice(notice("kb.card", "KB").to_wire())
    await _flush(bridge)
    assert run.updates == []


@pytest.mark.asyncio
async def test_initialize_advertises_banner_capability():
    agent = CodePuppyAgent()
    try:
        response = await agent.initialize(protocol_version=1, client_capabilities=None)
    finally:
        io_delegation.uninstall()
        permissions.uninstall()

    assert response.agent_capabilities.field_meta == BANNER_CAPABILITY
    wire = response.model_dump(by_alias=True, exclude_none=True)
    assert wire["agentCapabilities"]["_meta"]["code_puppy"]["banners"] == {"v": 1}


def test_agent_capabilities_returns_fresh_meta_per_call():
    first = capabilities.agent_capabilities()
    first.field_meta["code_puppy"]["banners"]["v"] = 99

    assert capabilities.agent_capabilities().field_meta == BANNER_CAPABILITY
