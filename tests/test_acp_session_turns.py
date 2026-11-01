"""ACPSession turn lifecycle: overlap guard, cancel, interrupted meta, binary media."""

from __future__ import annotations

import asyncio
import base64

import pytest
from acp import RequestError
from acp.schema import BlobResourceContents, EmbeddedResourceContentBlock
from pydantic_ai import BinaryContent

from code_puppy_core_plugins.acp import commands
from code_puppy_core_plugins.acp.session import (
    STOP_CANCELLED,
    STOP_END_TURN,
    STOP_REFUSAL,
)
from tests.acp_session_support import (  # noqa: F401 (autouse fixture)
    FakeAgent,
    install_binary_media,
    install_subagent_store,
    make_session,
    running_turn,
    text_block,
)

OVERLAP_ERROR_MESSAGE = "Session already has a running prompt"


def _overlap_error(session_id: str) -> dict:
    return {
        "code": -32000,
        "message": OVERLAP_ERROR_MESSAGE,
        "data": {"sessionId": session_id, "reason": "prompt_in_flight"},
    }


def _spy_final_result(monkeypatch: pytest.MonkeyPatch, session) -> list:
    sent: list = []

    async def record(result):
        sent.append(result)

    monkeypatch.setattr(session, "_send_final_result", record)
    return sent


def _record(agent_name: str, sub_session_id: str, saved: int) -> dict:
    return {
        "agent_name": agent_name,
        "session_id": sub_session_id,
        "saved_count": saved,
    }


def _interrupted_meta(agent_name: str, sub_session_id: str, saved: int) -> dict:
    record = {
        "agentName": agent_name,
        "subSessionId": sub_session_id,
        "savedCount": saved,
    }
    return {"codePuppy/interrupted": [record]}


async def _done(awaitable, timeout: float = 5):
    # A regressed overlap guard or stuck cancel must fail with TimeoutError, not hang.
    return await asyncio.wait_for(awaitable, timeout)


async def _start_first_turn(monkeypatch, session, first_behaviour):
    """Start a first prompt and return (task, release) once it holds the turn."""
    if first_behaviour == "slash-command":
        started = asyncio.Event()
        gate = asyncio.Event()

        async def slow_command(session_id, command):
            started.set()
            await gate.wait()

        monkeypatch.setattr(commands, "run", slow_command)
        task = asyncio.ensure_future(session.prompt([text_block("/help")]))
        await started.wait()
        return task, gate.set

    task = asyncio.ensure_future(session.prompt([text_block("go")]))
    while not session.agent.calls:
        await asyncio.sleep(0)
    return task, session.agent.release


# --- overlap guard ----------------------------------------------------


@pytest.mark.parametrize(
    ("first_behaviour", "first_stop", "runs_before_reject"),
    [
        ("block", STOP_END_TURN, 1),
        ("fail", STOP_REFUSAL, 1),
        ("slash-command", STOP_END_TURN, 0),
    ],
    ids=["first-completes", "first-fails", "slash-command-phase"],
)
async def test_overlapping_prompt_is_rejected_and_next_prompt_works(
    monkeypatch, first_behaviour, first_stop, runs_before_reject
):
    scripted = [] if first_behaviour == "slash-command" else [first_behaviour]
    session = make_session(
        "s1", FakeAgent(script=[*scripted, "finish"], history=["prior"])
    )
    task, release = await _start_first_turn(monkeypatch, session, first_behaviour)

    with pytest.raises(RequestError) as caught:
        await _done(session.prompt([text_block("second")]))
    assert caught.value.to_error_obj() == _overlap_error("s1")
    assert session.agent.get_message_history() == ["prior"]
    assert len(session.agent.calls) == runs_before_reject

    release()
    assert (await _done(task)).stop_reason == first_stop
    follow_up = await _done(session.prompt([text_block("third")]))
    assert follow_up.stop_reason == STOP_END_TURN
    assert len(session.agent.calls) == runs_before_reject + 1


# --- cancel ----------------------------------------------------------


@pytest.mark.parametrize(
    "cancelled_by",
    ["block", "permission"],
    ids=["streaming", "permission-request"],
)
async def test_cancelled_turn_reports_cancelled_and_next_turn_runs(
    monkeypatch, cancelled_by
):
    session = make_session("s1", FakeAgent(script=[cancelled_by, "finish"]))
    sent = _spy_final_result(monkeypatch, session)

    async with running_turn(session) as turn:
        assert session.agent.waiting_on == cancelled_by
        session.cancel()
        result = await _done(turn)

    assert result.stop_reason == STOP_CANCELLED
    assert result.field_meta is None
    assert sent == []
    follow_up = await _done(session.prompt([text_block("next")]))
    assert follow_up.stop_reason == STOP_END_TURN


async def test_cancel_after_the_task_finished_does_not_set_the_flag():
    session = make_session("s1", FakeAgent())

    finished = asyncio.ensure_future(asyncio.sleep(0))
    await finished
    session._task = finished
    session.cancel()
    assert session._cancel_requested is False


# --- interrupted sub-agent meta ---------------------------------------


async def test_cancelled_turn_reports_only_its_own_interrupted_subagents(monkeypatch):
    store = install_subagent_store(monkeypatch)
    store.records["A"] = [_record("planner", "sub-a1", 3)]
    store.records["B"] = [_record("reviewer", "sub-b1", 7)]
    session_a = make_session("A", FakeAgent(script=["block"]))
    session_b = make_session("B", FakeAgent(script=["block"]))

    async with running_turn(session_a) as turn_a, running_turn(session_b) as turn_b:
        session_a.cancel()
        result_a = await _done(turn_a)
        assert result_a.field_meta == _interrupted_meta("planner", "sub-a1", 3)
        session_b.cancel()
        result_b = await _done(turn_b)
        assert result_b.field_meta == _interrupted_meta("reviewer", "sub-b1", 7)

    assert store.drain_calls == []
    assert store.clear_calls == []


@pytest.mark.parametrize(
    ("setup", "cancel"),
    [
        ("normal-end-with-records", False),
        ("cancel-empty-store", True),
        ("cancel-old-core", True),
    ],
)
async def test_interrupted_meta_is_omitted_unless_cancelled_with_records(
    monkeypatch, setup, cancel
):
    if setup != "cancel-old-core":
        store = install_subagent_store(monkeypatch)
        if setup == "normal-end-with-records":
            store.records["A"] = [_record("planner", "sub-a1", 3)]
    session = make_session("A", FakeAgent(script=["block"] if cancel else []))
    sent = _spy_final_result(monkeypatch, session)

    if cancel:
        async with running_turn(session) as turn:
            session.cancel()
            result = await _done(turn)
    else:
        result = await session.prompt([text_block("hi")])

    assert result.stop_reason == (STOP_CANCELLED if cancel else STOP_END_TURN)
    assert result.field_meta is None
    assert len(sent) == (0 if cancel else 1)


# --- binary media threading -------------------------------------------

PDF_BYTES = b"%PDF-1.4 test"
NOTICE = (
    "[Attachment not sent: file:///docs/report.pdf (application/pdf) "
    "is not supported by the current model.]"
)


def _pdf_block() -> EmbeddedResourceContentBlock:
    blob = base64.b64encode(PDF_BYTES).decode()
    return EmbeddedResourceContentBlock(
        type="resource",
        resource=BlobResourceContents(
            uri="file:///docs/report.pdf", blob=blob, mime_type="application/pdf"
        ),
    )


@pytest.mark.parametrize(
    ("model", "new_core", "attached"),
    [
        ("m-pdf", True, True),
        ("m-text", True, False),
        ("m-pdf", False, False),
    ],
    ids=["supported-model", "unsupported-model", "old-core"],
)
async def test_embedded_pdf_reaches_model_only_when_supported(
    monkeypatch, model, new_core, attached
):
    if new_core:
        install_binary_media(monkeypatch, {"m-pdf": ["application/pdf"], "m-text": []})
    session = make_session("s1", FakeAgent(model_name=model))

    result = await session.prompt([_pdf_block()])

    assert result.stop_reason == STOP_END_TURN
    call = session.agent.calls[0]
    if attached:
        assert call["text"] == ""
        assert call["link_attachments"] is None
        assert len(call["attachments"]) == 1
        attachment = call["attachments"][0]
        assert isinstance(attachment, BinaryContent)
        assert attachment.data == PDF_BYTES
        assert attachment.media_type == "application/pdf"
    else:
        assert call["text"] == NOTICE
        assert call["attachments"] is None
