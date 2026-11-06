"""Timeout tests: a hung summarizer or sink fails its step without stalling the background pass."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from code_puppy_core_plugins.acp import feedback, feedback_summarizer
from code_puppy_core_plugins.acp import feedback_queue as queue
from tests.acp_feedback_support import clean_feedback_state  # noqa: F401
from tests.acp_feedback_support import feedback_data_dir  # noqa: F401
from tests.acp_feedback_support import sample_enqueue_kwargs

pytestmark = pytest.mark.usefixtures("feedback_data_dir", "clean_feedback_state")

SESSION_DOWN = "sess-timeout-down"
SESSION_UP = "sess-timeout-up"
CANARY = "canary-timeout-comment-4Zk"
SUMMARY = feedback_summarizer.FeedbackSummary(
    tags=["incorrect"], reason="generic cause"
)


class HungSummarizer:
    def __init__(self) -> None:
        self.hang = True
        self.never = asyncio.Event()
        self.calls: list[str] = []

    async def __call__(self, session_id: str, chips: Any = ()) -> Any:
        self.calls.append(session_id)
        if self.hang:
            await self.never.wait()
        return SUMMARY


class HangingSink:
    def __init__(self, hang_for: str | None) -> None:
        self.hang_for = hang_for
        self.never = asyncio.Event()
        self.records: list[dict[str, Any]] = []

    async def __call__(self, record: dict[str, Any]) -> bool:
        self.records.append(record)
        if record["id"] == self.hang_for:
            await self.never.wait()
        return True


@pytest.fixture
def fast_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feedback, "STEP_TIMEOUT_SECONDS", 0.05)


@pytest.fixture
def summarizer(monkeypatch: pytest.MonkeyPatch) -> HungSummarizer:
    fake = HungSummarizer()
    monkeypatch.setattr(feedback_summarizer, "summarize", fake)
    return fake


def _enqueue_down(session_id: str = SESSION_DOWN) -> str:
    kwargs = sample_enqueue_kwargs(
        session_id=session_id, rating="down", reasons=["wrong"], comment=CANARY
    )
    return queue.enqueue(**kwargs)["id"]


def _enqueue_up(session_id: str = SESSION_UP) -> str:
    kwargs = sample_enqueue_kwargs(
        session_id=session_id, rating="up", reasons=[], comment=None
    )
    return queue.enqueue(**kwargs)["id"]


async def _run_pass() -> None:
    feedback.schedule_processing()
    await asyncio.wait_for(feedback.drain(), timeout=5)


def _state(feedback_id: str) -> dict[str, Any]:
    record = queue.get(feedback_id)
    assert record is not None
    return record


def test_step_timeout_default_is_sixty_seconds() -> None:
    assert feedback.STEP_TIMEOUT_SECONDS == 60.0


async def test_hung_summarizer_leaves_entry_queued_and_later_entries_deliver(
    fast_timeout: None, summarizer: HungSummarizer
) -> None:
    sink = HangingSink(hang_for=None)
    feedback.register_feedback_sink(sink)
    down_id = _enqueue_down()
    up_id = _enqueue_up()

    await _run_pass()

    assert _state(down_id)["state"] == "queued"
    assert _state(up_id)["state"] == "delivered"
    assert [record["id"] for record in sink.records] == [up_id]
    assert feedback._in_flight == set()


async def test_later_pass_retries_timed_out_entry(
    fast_timeout: None, summarizer: HungSummarizer
) -> None:
    down_id = _enqueue_down()
    await _run_pass()

    summarizer.hang = False
    await _run_pass()

    assert _state(down_id)["state"] == "delivered"
    assert _state(down_id)["attempts"] == 2


async def test_hung_sink_keeps_summary_and_later_entries_deliver(
    fast_timeout: None, summarizer: HungSummarizer
) -> None:
    summarizer.hang = False
    sink = HangingSink(hang_for=None)
    feedback.register_feedback_sink(sink)
    down_id = _enqueue_down()
    up_id = _enqueue_up()
    sink.hang_for = down_id

    await _run_pass()

    assert _state(down_id)["state"] == "summarized"
    assert _state(up_id)["state"] == "delivered"

    sink.hang_for = None
    await _run_pass()

    assert _state(down_id)["state"] == "delivered"
    assert summarizer.calls == [SESSION_DOWN]


async def test_hung_first_sink_does_not_block_second_sink(
    fast_timeout: None, summarizer: HungSummarizer
) -> None:
    summarizer.hang = False
    down_id = _enqueue_down()
    first = HangingSink(hang_for=None)
    feedback.register_feedback_sink(first)
    second = HangingSink(hang_for=None)
    feedback.register_feedback_sink(second)
    first.hang_for = down_id

    await _run_pass()

    assert [record["id"] for record in first.records] == [down_id]
    assert [record["id"] for record in second.records] == [down_id]
    assert _state(down_id)["state"] == "summarized"


@pytest.mark.parametrize("step", ["summarizer", "sink"])
async def test_timeout_logs_only_exception_type_name(
    fast_timeout: None,
    summarizer: HungSummarizer,
    caplog: pytest.LogCaptureFixture,
    step: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    if step == "sink":
        summarizer.hang = False
        sink = HangingSink(hang_for=None)
        feedback.register_feedback_sink(sink)
        sink.hang_for = _enqueue_down()
    else:
        _enqueue_down()
    await _run_pass()

    assert "TimeoutError" in caplog.text
    assert CANARY not in caplog.text
