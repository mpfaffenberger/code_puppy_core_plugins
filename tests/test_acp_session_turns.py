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


async def _done(awaitable, timeout: float = 5):
    # A regressed overlap guard or stuck cancel must fail with TimeoutError, not hang.
    return await asyncio.wait_for(awaitable, timeout)


# --- overlap guard ----------------------------------------------------


@pytest.mark.parametrize(
    ("first_behaviour", "first_stop"),
    [("block", STOP_END_TURN), ("fail", STOP_REFUSAL)],
    ids=["first-completes", "first-fails"],
)
async def test_overlapping_prompt_is_rejected_and_next_prompt_works(
    first_behaviour, first_stop
):
    session = make_session(
        "s1", FakeAgent(script=[first_behaviour, "finish"], history=["prior"])
    )

    async with running_turn(session) as first:
        with pytest.raises(RequestError) as caught:
            await _done(session.prompt([text_block("second")]))
        assert caught.value.to_error_obj() == _overlap_error("s1")
        assert session.agent.get_message_history() == ["prior"]
        assert len(session.agent.calls) == 1

    assert (await _done(first)).stop_reason == first_stop
    follow_up = await session.prompt([text_block("third")])
    assert follow_up.stop_reason == STOP_END_TURN
    assert len(session.agent.calls) == 2


async def test_overlap_during_slash_command_phase_is_rejected(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_command(session_id, command):
        started.set()
        await release.wait()
        return None

    monkeypatch.setattr(commands, "run", slow_command)
    session = make_session("s1", FakeAgent())
    first = asyncio.ensure_future(session.prompt([text_block("/help")]))
    await started.wait()

    with pytest.raises(RequestError) as caught:
        await _done(session.prompt([text_block("hi")]))
    assert caught.value.to_error_obj() == _overlap_error("s1")

    release.set()
    assert (await _done(first)).stop_reason == STOP_END_TURN
    assert session.agent.calls == []
    assert (await session.prompt([text_block("hi")])).stop_reason == STOP_END_TURN


# --- cancelled stop reason --------------------------------------------


@pytest.mark.parametrize(
    "waiting_on", ["block", "permission"], ids=["streaming", "permission-request"]
)
async def test_cancel_while_run_waits_reports_cancelled(monkeypatch, waiting_on):
    session = make_session("s1", FakeAgent(script=[waiting_on]))
    sent = _spy_final_result(monkeypatch, session)

    async with running_turn(session) as turn:
        assert session.agent.waiting_on == waiting_on
        session.cancel()
        result = await _done(turn)

    assert result.stop_reason == STOP_CANCELLED
    assert result.field_meta is None
    assert sent == []


async def test_core_swallowing_cancel_still_reports_cancelled(monkeypatch):
    session = make_session("s1", FakeAgent(script=["swallow"]))
    sent = _spy_final_result(monkeypatch, session)

    async with running_turn(session) as turn:
        session.cancel()
        result = await _done(turn)

    assert result.stop_reason == STOP_CANCELLED
    assert sent == []
    assert session.agent.get_message_history() == ["msg:swallowed"]


@pytest.mark.parametrize("cancelled_by", ["block", "swallow"])
async def test_cancel_does_not_poison_the_next_turn(cancelled_by):
    session = make_session("s1", FakeAgent(script=[cancelled_by, "finish"]))

    async with running_turn(session) as turn:
        session.cancel()
        assert (await _done(turn)).stop_reason == STOP_CANCELLED

    assert (await session.prompt([text_block("next")])).stop_reason == STOP_END_TURN


async def test_cancel_when_idle_is_a_noop_and_next_turn_ends_normally():
    session = make_session("s1", FakeAgent())

    session.cancel()

    assert (await session.prompt([text_block("hi")])).stop_reason == STOP_END_TURN


async def test_cancel_after_task_finished_does_not_set_the_flag():
    session = make_session("s1", FakeAgent())
    done = asyncio.ensure_future(asyncio.sleep(0))
    await done
    session._task = done

    session.cancel()

    assert session._cancel_requested is False


async def test_final_result_is_sent_for_a_normal_unstreamed_turn(monkeypatch):
    session = make_session("s1", FakeAgent())
    sent = _spy_final_result(monkeypatch, session)

    result = await session.prompt([text_block("hi")])

    assert result.stop_reason == STOP_END_TURN
    assert len(sent) == 1


# --- interrupted sub-agent meta ------------------------------------------------


async def test_cancelled_turn_reports_only_its_own_interrupted_subagents(monkeypatch):
    store = install_subagent_store(monkeypatch)
    store.records["A"] = [_record("planner", "sub-a1", 3)]
    store.records["B"] = [_record("reviewer", "sub-b1", 7)]
    session_a = make_session("A", FakeAgent(script=["block"]))
    session_b = make_session("B", FakeAgent(script=["block"]))

    async with running_turn(session_a) as turn_a, running_turn(session_b) as turn_b:
        session_a.cancel()
        result_a = await _done(turn_a)
        assert result_a.field_meta == {
            "codePuppy/interrupted": [
                {"agentName": "planner", "subSessionId": "sub-a1", "savedCount": 3}
            ]
        }
        session_b.cancel()
        result_b = await _done(turn_b)
        assert result_b.field_meta == {
            "codePuppy/interrupted": [
                {"agentName": "reviewer", "subSessionId": "sub-b1", "savedCount": 7}
            ]
        }

    assert store.drain_calls == []
    assert store.clear_calls == []


async def test_normally_ended_turn_omits_interrupted_meta(monkeypatch):
    store = install_subagent_store(monkeypatch)
    store.records["A"] = [_record("planner", "sub-a1", 3)]

    result = await make_session("A").prompt([text_block("hi")])

    assert result.stop_reason == STOP_END_TURN
    assert result.field_meta is None
    assert store.drain_calls == []


@pytest.mark.parametrize("setup", ["empty-store", "old-core"])
async def test_cancelled_turn_without_interrupted_records_omits_meta(
    monkeypatch, setup
):
    if setup == "empty-store":
        install_subagent_store(monkeypatch)
    session = make_session("A", FakeAgent(script=["block"]))

    async with running_turn(session) as turn:
        session.cancel()
        result = await _done(turn)

    assert result.stop_reason == STOP_CANCELLED
    assert result.field_meta is None


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
