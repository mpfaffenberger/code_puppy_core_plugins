"""ACP session cancellation: a cancelled turn reports ``cancelled`` even when core
swallows the cancel, and SDK shutdown cancelling the prompt handler propagates.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List

import pytest

from code_puppy_core_plugins.acp import persistence
from code_puppy_core_plugins.acp.session import (
    STOP_CANCELLED,
    STOP_END_TURN,
    ACPSession,
)


class _Result:
    """Minimal run result: no usage, no message history."""

    output = "done"

    def all_messages(self) -> List[Any]:
        return []


class _FakeAgent:
    """Stands in for the agent. With ``hold``, ``run_with_mcp`` blocks until cancelled."""

    def __init__(self, *, hold: bool, swallow_cancel: bool = False) -> None:
        self.hold = hold
        self.swallow_cancel = swallow_cancel
        self.started = asyncio.Event()

    async def run_with_mcp(self, text, attachments=None, link_attachments=None):
        self.started.set()
        if not self.hold:
            return _Result()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if self.swallow_cancel:
                return _Result()
            raise
        return _Result()

    def set_message_history(self, messages: List[Any]) -> None:
        pass


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(persistence, "save", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "code_puppy.messaging.set_session_context", lambda *args, **kwargs: None
    )


def _session(agent: _FakeAgent) -> ACPSession:
    return ACPSession("s1", agent)


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "swallow_cancel", [True, False], ids=["core_swallows", "propagates"]
)
async def test_cancel_during_run_reports_cancelled(swallow_cancel):
    agent = _FakeAgent(hold=True, swallow_cancel=swallow_cancel)
    session = _session(agent)

    turn = asyncio.ensure_future(session.prompt([_text("go")]))
    await agent.started.wait()
    session.cancel()

    result = await asyncio.wait_for(turn, 2)
    assert result.stop_reason == STOP_CANCELLED


@pytest.mark.asyncio
async def test_normal_turn_ends_turn():
    session = _session(_FakeAgent(hold=False))

    result = await session.prompt([_text("hi")])

    assert result.stop_reason == STOP_END_TURN


@pytest.mark.asyncio
async def test_idle_cancel_does_not_leak_into_next_turn():
    session = _session(_FakeAgent(hold=False))
    session.cancel()

    result = await session.prompt([_text("hi")])

    assert result.stop_reason == STOP_END_TURN


@pytest.mark.asyncio
async def test_cancelled_turn_does_not_leak_into_next_turn():
    agent = _FakeAgent(hold=True, swallow_cancel=True)
    session = _session(agent)
    turn = asyncio.ensure_future(session.prompt([_text("go")]))
    await agent.started.wait()
    session.cancel()
    assert (await asyncio.wait_for(turn, 2)).stop_reason == STOP_CANCELLED

    agent.hold = False
    result = await session.prompt([_text("next")])

    assert result.stop_reason == STOP_END_TURN


@pytest.mark.asyncio
async def test_shutdown_cancel_of_prompt_handler_propagates():
    agent = _FakeAgent(hold=True, swallow_cancel=True)
    session = _session(agent)
    handler = asyncio.ensure_future(session.prompt([_text("go")]))
    await agent.started.wait()
    inner = session._task
    assert inner is not None

    # Simulates the SDK closing the connection while the prompt handler awaits.
    handler.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(handler, 2)
    assert inner.done()
