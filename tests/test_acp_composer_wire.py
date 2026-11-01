"""In-process ACP JSON-RPC wire test through the real SDK (``acp.run_agent``).

Raw ndjson over a socketpair, so assertions see the exact wire JSON. Runs on
both cores: new-core-only expectations branch on the composer_ext probes.
Only the agent run and the disk/IO side effects are stubbed.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import socket
from typing import Any, AsyncIterator

import pytest
from acp import run_agent

import code_puppy.config as config
from code_puppy_core_plugins.acp import (
    composer_ext,
    io_delegation,
    permissions,
    persistence,
    session_config,
    state,
)
from code_puppy_core_plugins.acp.agent import CodePuppyAgent
from tests.acp_session_support import FakeAgent

TIMEOUT = 10
CATALOG_TIMEOUT = 600  # bounded only to fail a hang; cold plugin discovery runs once per process and is slow on a loaded machine
METHOD_NOT_FOUND = -32601
PROMPT_IN_FLIGHT = -32000
STEER_METHOD = "_code_puppy/session/steer"
STEER_SUPPORTED = composer_ext.steer_supported()
BINARY_SUPPORTED = composer_ext.binary_media_meta(None) is not None
EXPECTED_CAPABILITY_KEYS = {
    key
    for key, on in (
        ("codePuppy/steer", STEER_SUPPORTED),
        ("codePuppy/binaryMediaTypes", BINARY_SUPPORTED),
    )
    if on
}
TEXT = {"type": "text", "text": "go"}
IMAGE = {"type": "image", "data": "aGk=", "mimeType": "image/png"}


class Wire:
    """Raw ndjson JSON-RPC client; keeps only responses, ignores agent notifications."""

    def __init__(self, reader: asyncio.StreamReader, writer: Any, fakes: list) -> None:
        self.fakes = fakes
        self.session_ids: list[str] = []
        self._reader = reader
        self._writer = writer
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._pump_task = asyncio.ensure_future(self._pump())

    async def _pump(self) -> None:
        while line := await self._reader.readline():
            message = json.loads(line)
            if "method" not in message and message.get("id") in self._pending:
                self._pending.pop(message["id"]).set_result(message)

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        return await asyncio.wait_for(future, TIMEOUT)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, payload: dict[str, Any]) -> None:
        self._writer.write(json.dumps(payload).encode() + b"\n")
        await asyncio.wait_for(self._writer.drain(), TIMEOUT)

    async def close(self) -> None:
        self._pump_task.cancel()
        self._writer.close()


@pytest.fixture
async def wire(monkeypatch) -> AsyncIterator[Wire]:
    fakes: list[FakeAgent] = []

    def new_fake_agent() -> FakeAgent:
        fake = FakeAgent(script=["block"] * 4)
        fakes.append(fake)
        return fake

    monkeypatch.setattr(CodePuppyAgent, "_new_agent", staticmethod(new_fake_agent))
    monkeypatch.setattr(
        session_config, "config_options", lambda: [session_config._mode_option()]
    )
    monkeypatch.setattr(io_delegation, "install", lambda caps: None)
    monkeypatch.setattr(persistence, "save", lambda *args, **kwargs: None)
    monkeypatch.setattr(persistence, "delete", lambda session_id: None)
    monkeypatch.setattr(persistence, "load_history", lambda session_id: None)
    monkeypatch.setattr(config, "save_command_to_history", lambda text: None)
    # Cold catalog load is synchronous; pay it off-loop before serving.
    await asyncio.wait_for(
        asyncio.to_thread(composer_ext.available_commands), CATALOG_TIMEOUT
    )

    client_sock, agent_sock = socket.socketpair()
    agent_reader, agent_writer = await asyncio.wait_for(
        asyncio.open_connection(sock=agent_sock), TIMEOUT
    )
    client_reader, client_writer = await asyncio.wait_for(
        asyncio.open_connection(sock=client_sock), TIMEOUT
    )
    agent = CodePuppyAgent()
    serving = asyncio.ensure_future(run_agent(agent, agent_writer, agent_reader))
    client = Wire(client_reader, client_writer, fakes)
    try:
        yield client
    finally:
        for session_id in client.session_ids:
            await asyncio.wait_for(agent.close_session(session_id), TIMEOUT)
        await client.close()
        await asyncio.wait_for(serving, TIMEOUT)
        agent.shutdown()
        permissions.uninstall()
        state.set_connection(None, None)
        await asyncio.sleep(0.2)  # let deferred command announcements see no connection


async def _until(predicate) -> None:
    async def spin() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(spin(), TIMEOUT)


def _result(reply: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in reply, reply
    return reply["result"]


async def _new_session(client: Wire) -> str:
    session_id = _result(
        await client.call("session/new", {"cwd": "/w", "mcpServers": []})
    )["sessionId"]
    client.session_ids.append(session_id)
    return session_id


async def _start_prompt(client: Wire, session_id: str, blocks=(TEXT,)) -> asyncio.Task:
    """Send a prompt and wait until its (blocked) agent run is in flight."""
    task = asyncio.ensure_future(
        client.call("session/prompt", {"sessionId": session_id, "prompt": list(blocks)})
    )
    await _until(lambda: client.fakes[-1].calls)
    return task


async def _finish_prompt(client: Wire, task: asyncio.Task) -> dict[str, Any]:
    client.fakes[-1].release()
    return await asyncio.wait_for(task, TIMEOUT)


# ---- initialize / session/new ---------------------------------------------
async def test_initialize_advertises_composer_meta_on_the_wire(wire):
    caps = _result(await wire.call("initialize", {"protocolVersion": 1}))[
        "agentCapabilities"
    ]

    assert set(caps.get("_meta") or {}) == EXPECTED_CAPABILITY_KEYS


async def test_session_new_meta_on_the_wire(wire):
    result = _result(await wire.call("session/new", {"cwd": "/w", "mcpServers": []}))
    wire.session_ids.append(result["sessionId"])

    if BINARY_SUPPORTED:
        assert set(result["_meta"]) == {"codePuppy/binaryMediaTypes"}
        assert isinstance(result["_meta"]["codePuppy/binaryMediaTypes"], list)
    else:
        assert "_meta" not in result


# ---- prompt concurrency and cancellation ----------------------------------
async def test_second_prompt_while_running_is_rejected_and_first_completes(wire):
    session_id = await _new_session(wire)
    first = await _start_prompt(wire, session_id)

    second = await wire.call(
        "session/prompt", {"sessionId": session_id, "prompt": [TEXT]}
    )

    assert "result" not in second
    assert second["error"]["code"] == PROMPT_IN_FLIGHT
    assert second["error"]["message"] == "Session already has a running prompt"
    assert second["error"]["data"] == {
        "sessionId": session_id,
        "reason": "prompt_in_flight",
    }
    assert _result(await _finish_prompt(wire, first))["stopReason"] == "end_turn"


async def test_cancel_notification_ends_prompt_as_cancelled(wire):
    session_id = await _new_session(wire)
    running = await _start_prompt(wire, session_id)

    await wire.notify("session/cancel", {"sessionId": session_id})
    result = _result(await asyncio.wait_for(running, TIMEOUT))

    assert result["stopReason"] == "cancelled"
    assert "_meta" not in result  # no interrupted records queued


# ---- _code_puppy/session/steer extension request ---------------------------
@pytest.mark.parametrize(
    ("target", "blocks", "expected"),
    [
        pytest.param("running", [TEXT], {"accepted": True}, id="running-text"),
        pytest.param(
            "running",
            [IMAGE],
            {"accepted": False, "reason": "unsupported_content"},
            id="image-block",
        ),
        pytest.param(
            "missing",
            [TEXT],
            {"accepted": False, "reason": "not_running"},
            id="unknown-session",
        ),
    ],
)
async def test_steer_extension_request_on_the_wire(wire, target, blocks, expected):
    session_id = await _new_session(wire)
    running = await _start_prompt(wire, session_id) if target == "running" else None
    steer_target = session_id if target == "running" else "sess_missing"

    reply = await wire.call(STEER_METHOD, {"sessionId": steer_target, "prompt": blocks})

    if STEER_SUPPORTED:
        assert _result(reply) == expected
    else:
        assert reply["error"]["code"] == METHOD_NOT_FOUND
    if running is not None:
        await _finish_prompt(wire, running)


async def test_unknown_extension_method_is_method_not_found(wire):
    reply = await wire.call("_code_puppy/nope", {"sessionId": "sess_x", "prompt": []})

    assert reply["error"]["code"] == METHOD_NOT_FOUND
