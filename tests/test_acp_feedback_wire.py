"""JSON-RPC wire contract for ACP reply feedback and turn ``_meta``.

Covers agent.py :: CodePuppyAgent.initialize()/prompt()/ext_method()/on_connect() as framed by
the SDK's AgentSideConnection, so assertions see exactly what an ACP client receives.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from acp.agent.connection import AgentSideConnection

from code_puppy_core_plugins.acp import (
    feedback,
    feedback_queue,
    feedback_summarizer,
    io_delegation,
    permissions,
    persistence,
    state,
    turn_metadata,
)
from code_puppy_core_plugins.acp.agent import CodePuppyAgent
from code_puppy_core_plugins.acp.feedback_summarizer import FeedbackSummary
from code_puppy_core_plugins.acp.session import ACPSession
from tests.acp_feedback_support import clean_feedback_state  # noqa: F401
from tests.acp_feedback_support import feedback_data_dir  # noqa: F401

pytestmark = pytest.mark.usefixtures("feedback_data_dir", "clean_feedback_state")

WIRE_TIMEOUT = 10
SENTINEL = "SENTINEL-never-echo"
SUMMARY = FeedbackSummary(tags=["incorrect"], reason="Generic cause")
PROMPT_PARAMS = {"sessionId": "sess_x", "prompt": [{"type": "text", "text": "hi"}]}
FEEDBACK_META = {
    "codePuppy": {
        "feedback": {"method": "_code_puppy/feedback", "version": 1},
    }
}


class WireClient:
    """Acts as the ACP client: newline-delimited JSON-RPC over the agent's socket."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._next_id = 0

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        await self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        return await asyncio.wait_for(self._response(request_id), WIRE_TIMEOUT)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, message: dict[str, Any]) -> None:
        self._writer.write(json.dumps(message).encode() + b"\n")
        await self._writer.drain()

    async def _response(self, request_id: int) -> dict[str, Any]:
        # Skips agent notifications (session/update) and anything not answering this id.
        while True:
            line = await self._reader.readline()
            if not line:
                raise ConnectionError("agent closed the wire")
            message = json.loads(line)
            if message.get("id") == request_id and "method" not in message:
                return message


def _result(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response, response
    return response["result"]


class _Wire:
    """One agent-side connection wired to a client over a socketpair."""

    def __init__(self, agent: CodePuppyAgent, pipes: tuple[Any, ...]) -> None:
        agent_reader, agent_writer, client_reader, client_writer = pipes
        self.client = WireClient(client_reader, client_writer)
        self._writers = [agent_writer, client_writer]
        # listening=False + listen() mirrors acp.run_agent; on_connect runs in __init__.
        self._connection = AgentSideConnection(
            agent, agent_writer, agent_reader, listening=False
        )
        self._listen = asyncio.create_task(self._connection.listen())

    async def close(self) -> None:
        await self._connection.close()
        self._listen.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._listen
        for writer in self._writers:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


async def _open_pipes() -> tuple[Any, ...]:
    agent_sock, client_sock = socket.socketpair()
    agent_reader, agent_writer = await asyncio.open_connection(sock=agent_sock)
    client_reader, client_writer = await asyncio.open_connection(sock=client_sock)
    return agent_reader, agent_writer, client_reader, client_writer


class _FakeResult:
    output = "done"

    def all_messages(self) -> list[Any]:
        return []


class _FakeAgent:
    """Stands in for a Code Puppy agent; an Exception ``model_name`` is raised on lookup."""

    def __init__(self, model_name: Any = "model-wire", *, block: bool = False) -> None:
        self._model_name = model_name
        self._block = block
        self._history: list[Any] = []
        self.started = asyncio.Event()

    async def run_with_mcp(
        self, text: str, attachments: Any = None, link_attachments: Any = None
    ) -> _FakeResult:
        self.started.set()
        if self._block:
            await asyncio.Event().wait()
        return _FakeResult()

    def get_model_name(self) -> Any:
        if isinstance(self._model_name, Exception):
            raise self._model_name
        return self._model_name

    def get_message_history(self) -> list[Any]:
        return list(self._history)

    def set_message_history(self, history: list[Any]) -> None:
        self._history = list(history)


def _inject_session(agent: CodePuppyAgent, fake: _FakeAgent) -> None:
    agent._sessions["sess_x"] = ACPSession("sess_x", fake)


@pytest.fixture(autouse=True)
def triage(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Guard: no test reaches a real model; the mock is also the assertion target."""
    mock = AsyncMock(return_value=SUMMARY)
    monkeypatch.setattr(feedback_summarizer, "summarize", mock)
    return mock


@pytest.fixture(autouse=True)
def _no_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(persistence, "save", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _clean_turn_providers() -> Any:
    saved = list(turn_metadata._providers)
    turn_metadata._providers.clear()
    yield
    turn_metadata._providers[:] = saved


@pytest.fixture
def agent() -> Any:
    instance = CodePuppyAgent()
    yield instance
    instance.shutdown()
    io_delegation.uninstall()
    permissions.uninstall()
    state.set_connection(None, None)


@pytest.fixture
async def open_wire(
    agent: CodePuppyAgent,
) -> AsyncIterator[Callable[[CodePuppyAgent], Awaitable[WireClient]]]:
    opened: list[_Wire] = []

    async def _open(target: CodePuppyAgent) -> WireClient:
        # No await after construction: on_connect's scheduled retry cannot run before return.
        wire = _Wire(target, await _open_pipes())
        opened.append(wire)
        return wire.client

    yield _open
    for wire in reversed(opened):
        await wire.close()


# ---- a. handshake -----------------------------------------------------------
async def test_initialize_advertises_feedback_extension(open_wire, agent):
    client = await open_wire(agent)

    result = _result(await client.request("initialize", {"protocolVersion": 1}))

    assert result["agentCapabilities"]["_meta"] == FEEDBACK_META


# ---- b. feedback over the wire ---------------------------------------------
VALID_FEEDBACK = [
    pytest.param(
        {"rating": "down", "reasons": ["wrong", "other"], "comment": "bad\nanswer\t!"},
        "bad answer !",
        id="down-sanitized-comment",
    ),
    pytest.param({"rating": "up"}, None, id="up"),
]


@pytest.mark.parametrize("extra, stored_comment", VALID_FEEDBACK)
async def test_valid_feedback_is_accepted_and_persisted(
    open_wire, agent, extra, stored_comment
):
    client = await open_wire(agent)

    result = _result(
        await client.request(
            "_code_puppy/feedback", {"sessionId": "sess_feedback", **extra}
        )
    )

    feedback_id = result["feedbackId"]
    assert result["accepted"] is True
    assert str(uuid.UUID(feedback_id)) == feedback_id
    assert feedback_queue.get(feedback_id)["comment"] == stored_comment


@pytest.mark.parametrize(
    "extra, expected_reason",
    [
        pytest.param(
            {"rating": "down", "reasons": ["wrong", "other"], "comment": "bad answer"},
            "Generic cause",
            id="down-triaged",
        ),
        pytest.param({"rating": "up"}, None, id="up-untriaged"),
    ],
)
async def test_feedback_reaches_sink_after_background_processing(
    open_wire, agent, triage, extra, expected_reason
):
    delivered: list[dict[str, Any]] = []

    async def sink(record: dict[str, Any]) -> bool:
        delivered.append(record)
        return True

    feedback.register_feedback_sink(sink)
    client = await open_wire(agent)

    result = _result(
        await client.request(
            "_code_puppy/feedback", {"sessionId": "sess_feedback", **extra}
        )
    )
    await feedback.drain()

    assert [record["id"] for record in delivered] == [result["feedbackId"]]
    assert delivered[0].get("reason") == expected_reason
    if extra["rating"] == "down":
        triage.assert_awaited_once_with("sess_feedback", ["wrong", "other"])
    else:
        triage.assert_not_awaited()


# ---- c. invalid params ------------------------------------------------------
INVALID_FEEDBACK = [
    pytest.param({"sessionId": "sess_bad", "rating": "meh"}, id="bad-rating"),
    pytest.param(
        {"sessionId": "sess_bad", "rating": "down", "reasons": ["other"]},
        id="other-without-comment",
    ),
    pytest.param(
        {
            "sessionId": "sess_bad",
            "rating": "down",
            "reasons": ["wrong"],
            "comment": f"{SENTINEL}{'x' * (255 - len(SENTINEL))}",
        },
        id="comment-255-chars",
    ),
    pytest.param(
        {"rating": "down", "reasons": ["wrong"], "comment": SENTINEL},
        id="missing-session-id",
    ),
    pytest.param(
        {"sessionId": "sess_bad", "rating": "up", "reasons": ["wrong"]},
        id="up-with-reasons",
    ),
]


@pytest.mark.parametrize("params", INVALID_FEEDBACK)
async def test_invalid_feedback_is_rejected_with_invalid_params(
    open_wire, agent, params
):
    client = await open_wire(agent)

    response = await client.request("_code_puppy/feedback", params)

    assert response["error"]["code"] == -32602
    assert SENTINEL not in json.dumps(response)
    assert feedback_queue.read_all() == []


# ---- d. unknown extension ---------------------------------------------------
async def test_unknown_extension_is_method_not_found_and_standard_methods_work(
    open_wire, agent
):
    client = await open_wire(agent)

    error = (await client.request("_code_puppy/does_not_exist", {}))["error"]
    result = _result(await client.request("initialize", {"protocolVersion": 1}))

    assert error["code"] == -32601
    assert "agentCapabilities" in result


# ---- e. session/prompt turn _meta ------------------------------------------
TURN_META_CASES = [
    pytest.param(
        "model-wire",
        {"correlationId": "corr-wire"},
        {"codePuppy": {"turn": {"model": "model-wire", "correlationId": "corr-wire"}}},
        id="model-and-correlation",
    ),
    pytest.param(
        "model-wire",
        None,
        {"codePuppy": {"turn": {"model": "model-wire"}}},
        id="model-only",
    ),
    pytest.param(RuntimeError("no model"), None, None, id="nothing-known-omits-meta"),
]


@pytest.mark.parametrize("model_name, provided, expected_meta", TURN_META_CASES)
async def test_prompt_response_carries_turn_meta_on_wire(
    open_wire, agent, model_name, provided, expected_meta
):
    _inject_session(agent, _FakeAgent(model_name))
    if provided is not None:
        turn_metadata.register_turn_metadata_provider(lambda _a, _s: provided)
    client = await open_wire(agent)

    result = _result(await client.request("session/prompt", PROMPT_PARAMS))

    assert result["stopReason"] == "end_turn"
    if expected_meta is None:
        assert "_meta" not in result
    else:
        assert result["_meta"] == expected_meta


async def test_cancelled_turn_still_reports_cancelled_with_model_meta(open_wire, agent):
    fake = _FakeAgent(block=True)
    _inject_session(agent, fake)
    client = await open_wire(agent)
    prompt = asyncio.create_task(client.request("session/prompt", PROMPT_PARAMS))
    await asyncio.wait_for(fake.started.wait(), WIRE_TIMEOUT)

    await client.notify("session/cancel", {"sessionId": "sess_x"})
    result = _result(await asyncio.wait_for(prompt, WIRE_TIMEOUT))

    assert result["stopReason"] == "cancelled"
    assert result["_meta"] == {"codePuppy": {"turn": {"model": "model-wire"}}}


# ---- f. agent-level units ---------------------------------------------------
@pytest.mark.parametrize(
    "session_id, model_name, expected",
    [
        pytest.param("sess_missing", "model-x", None, id="unknown-session"),
        pytest.param("sess_x", RuntimeError("boom"), None, id="raising-agent"),
        pytest.param("sess_x", "", None, id="empty-name"),
        pytest.param("sess_x", 42, None, id="non-string-name"),
        pytest.param("sess_x", "model-x", "model-x", id="known-model"),
    ],
)
def test_model_for_session_never_raises(agent, session_id, model_name, expected):
    _inject_session(agent, _FakeAgent(model_name))

    assert agent._model_for_session(session_id) == expected


@pytest.mark.parametrize(
    "side_effect", [None, RuntimeError("boom")], ids=["schedules", "schedule-raises"]
)
async def test_on_connect_schedules_feedback_retry_without_raising(
    agent, monkeypatch, side_effect
):
    schedule = Mock(side_effect=side_effect)
    monkeypatch.setattr(feedback, "schedule_processing", schedule)

    agent.on_connect(object())

    schedule.assert_called_once_with()


async def test_unfinished_feedback_is_retried_at_launch_without_blocking_startup(
    open_wire, agent
):
    queued = feedback_queue.enqueue(
        session_id="sess_retry",
        correlation_id=None,
        rating="up",
        reasons=[],
        comment=None,
        model=None,
    )
    delivered: list[str] = []

    async def sink(record: dict[str, Any]) -> bool:
        delivered.append(record["id"])
        return True

    feedback.register_feedback_sink(sink)

    await open_wire(agent)  # on_connect schedules the retry and returns
    assert delivered == []

    await feedback.drain()
    assert delivered == [queued["id"]]
    assert feedback_queue.get(queued["id"])["state"] == feedback_queue.STATE_DELIVERED
