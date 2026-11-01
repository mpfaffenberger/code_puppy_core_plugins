"""Flow tests: handler -> durable queue -> background triage and delivery."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from typing import Any

import pytest
from acp import RequestError

from code_puppy_core_plugins.acp import feedback, feedback_queue as queue
from code_puppy_core_plugins.acp import feedback_summarizer
from tests.acp_feedback_support import clean_feedback_state  # noqa: F401
from tests.acp_feedback_support import feedback_data_dir  # noqa: F401
from tests.acp_feedback_support import sample_enqueue_kwargs

pytestmark = pytest.mark.usefixtures("feedback_data_dir", "clean_feedback_state")

SESSION = "sess-flow-1"
CANARY = "canary-comment-7Qx"
SUMMARY = feedback_summarizer.FeedbackSummary(
    tags=["incorrect"], reason="generic cause"
)
TRIAGE_TAG: ContextVar[str | None] = ContextVar("triage_tag", default=None)


class FakeSummarizer:
    def __init__(self) -> None:
        self.result: feedback_summarizer.FeedbackSummary | None = SUMMARY
        self.error: BaseException | None = None
        self.gate: asyncio.Event | None = None
        self.on_call: Callable[[], None] | None = None
        self.calls: list[tuple[str, list[str]]] = []

    async def __call__(self, session_id: str, chips: Any = ()) -> Any:
        self.calls.append((session_id, list(chips)))
        if self.on_call is not None:
            self.on_call()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.result


class FakeSink:
    def __init__(self, result: Any = True, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.on_call: Callable[[dict[str, Any]], None] | None = None
        self.records: list[dict[str, Any]] = []

    async def __call__(self, record: dict[str, Any]) -> Any:
        self.records.append(record)
        if self.on_call is not None:
            self.on_call(record)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def summarizer(monkeypatch: pytest.MonkeyPatch) -> FakeSummarizer:
    fake = FakeSummarizer()
    monkeypatch.setattr(feedback_summarizer, "summarize", fake)
    return fake


def _add_sink(sink: FakeSink) -> FakeSink:
    feedback.register_feedback_sink(sink)
    return sink


def _params(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sessionId": SESSION,
        "rating": "down",
        "reasons": ["wrong"],
        "comment": CANARY,
    }
    return {**base, **overrides}


async def _send(model_for_session: Any = None, **overrides: Any) -> dict[str, Any]:
    return await asyncio.wait_for(
        feedback.handle_ext_method(
            feedback.EXT_NAME,
            _params(**overrides),
            model_for_session=model_for_session,
        ),
        timeout=5,
    )


def _state(feedback_id: str) -> dict[str, Any]:
    record = queue.get(feedback_id)
    assert record is not None
    return record


async def test_handler_returns_without_awaiting_triage(
    summarizer: FakeSummarizer,
) -> None:
    gate = asyncio.Event()
    summarizer.gate = gate

    result = await _send()

    feedback_id = result["feedbackId"]
    assert result == {"accepted": True, "feedbackId": feedback_id}
    assert _state(feedback_id)["state"] == "queued"
    gate.set()
    await feedback.drain()
    assert _state(feedback_id)["state"] == "delivered"


async def test_enqueue_is_durable_before_scheduling_and_triage(
    monkeypatch: pytest.MonkeyPatch, summarizer: FakeSummarizer
) -> None:
    events: list[str] = []
    real_enqueue = queue.enqueue
    real_schedule = feedback.schedule_processing

    def enqueue_spy(**kwargs: Any) -> dict[str, Any]:
        events.append("enqueue")
        return real_enqueue(**kwargs)

    def schedule_spy() -> None:
        events.append("schedule")
        real_schedule()

    monkeypatch.setattr(queue, "enqueue", enqueue_spy)
    monkeypatch.setattr(feedback, "schedule_processing", schedule_spy)
    seen: dict[str, list[dict[str, Any]]] = {}
    summarizer.on_call = lambda: seen.setdefault("summarizer", queue.read_all())
    sink = _add_sink(FakeSink())
    sink.on_call = lambda record: seen.setdefault(
        "sink", [queue.get(record["id"]) or {}]
    )

    await _send()
    await feedback.drain()

    assert events == ["enqueue", "schedule"]
    assert seen["summarizer"][0]["state"] == "queued"
    assert seen["sink"][0]["state"] == "summarized"
    assert seen["sink"][0]["tags"] == ["incorrect"]
    assert seen["sink"][0]["reason"] == "generic cause"


async def test_down_is_triaged_then_delivered_with_full_record(
    summarizer: FakeSummarizer,
) -> None:
    sink = _add_sink(FakeSink())

    result = await _send(model_for_session=lambda session_id: "model-x")
    await feedback.drain()

    feedback_id = result["feedbackId"]
    assert summarizer.calls == [(SESSION, ["wrong"])]
    (record,) = sink.records
    assert record["id"] == feedback_id
    assert record["state"] == "summarized"
    assert record["comment"] == CANARY
    assert record["model"] == "model-x"
    assert record["reasons"] == ["wrong"]
    assert record["rating"] == "down"
    assert record["sessionId"] == SESSION
    final = _state(feedback_id)
    assert final["state"] == "delivered"
    assert final["tags"] == ["incorrect"]
    assert final["reason"] == "generic cause"
    assert final["attempts"] == 1


async def test_up_skips_triage_and_delivers(summarizer: FakeSummarizer) -> None:
    sink = _add_sink(FakeSink())

    feedback_id = (await _send(rating="up", reasons=None, comment=None))["feedbackId"]
    await feedback.drain()

    assert summarizer.calls == []
    assert sink.records[0]["state"] == "queued"
    final = _state(feedback_id)
    assert final["state"] == "delivered"
    assert final["tags"] == []
    assert final["attempts"] == 1


async def test_no_sinks_registered_still_delivers(summarizer: FakeSummarizer) -> None:
    feedback_id = (await _send())["feedbackId"]
    await feedback.drain()
    assert _state(feedback_id)["state"] == "delivered"


async def test_unknown_ext_method_is_method_not_found() -> None:
    with pytest.raises(RequestError) as info:
        await feedback.handle_ext_method("code_puppy/other", _params())
    assert info.value.code == -32601
    assert info.value.data == {"method": "_code_puppy/other"}


async def test_model_for_session_is_stored() -> None:
    feedback_id = (await _send(lambda session_id: "model-y"))["feedbackId"]
    assert _state(feedback_id)["model"] == "model-y"


async def test_raising_model_lookup_still_accepts_with_null_model() -> None:
    def boom(session_id: str) -> str:
        raise RuntimeError(f"lookup failed for {session_id}")

    feedback_id = (await _send(boom))["feedbackId"]

    assert _state(feedback_id)["model"] is None


async def test_enqueue_failure_is_internal_error_and_schedules_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: list[int] = []

    def failing_enqueue(**kwargs: Any) -> dict[str, Any]:
        raise OSError("disk full")

    monkeypatch.setattr(queue, "enqueue", failing_enqueue)
    monkeypatch.setattr(feedback, "schedule_processing", lambda: scheduled.append(1))

    with pytest.raises(RequestError) as info:
        await _send()

    assert info.value.code == -32603
    assert info.value.data == {"problem": "feedback could not be saved"}
    assert scheduled == []


@pytest.mark.parametrize("failure", ["no_summary", "raises"])
async def test_triage_failure_keeps_entry_queued(
    summarizer: FakeSummarizer, failure: str
) -> None:
    sink = _add_sink(FakeSink())
    if failure == "no_summary":
        summarizer.result = None
    else:
        summarizer.error = RuntimeError("triage backend down")

    feedback_id = (await _send())["feedbackId"]
    await feedback.drain()

    assert _state(feedback_id)["state"] == "queued"
    assert sink.records == []


@pytest.mark.parametrize(
    "sink_behaviour",
    [{"result": False}, {"error": RuntimeError("sink down")}],
    ids=["returns_false", "raises"],
)
async def test_sink_failure_keeps_summary_and_skips_retriage(
    summarizer: FakeSummarizer, sink_behaviour: dict[str, Any]
) -> None:
    sink = _add_sink(FakeSink(**sink_behaviour))
    feedback_id = (await _send())["feedbackId"]
    await feedback.drain()

    stored = _state(feedback_id)
    assert stored["state"] == "summarized"
    assert stored["tags"] == ["incorrect"]

    sink.result, sink.error = True, None
    feedback.schedule_processing()
    await feedback.drain()

    final = _state(feedback_id)
    assert final["state"] == "delivered"
    assert final["attempts"] == 2
    assert len(summarizer.calls) == 1


async def test_one_failing_sink_blocks_delivery_until_all_succeed(
    summarizer: FakeSummarizer,
) -> None:
    _add_sink(FakeSink(result=True))
    second = _add_sink(FakeSink(result=False))
    feedback_id = (await _send())["feedbackId"]
    await feedback.drain()

    assert _state(feedback_id)["state"] == "summarized"

    second.result = True
    feedback.schedule_processing()
    await feedback.drain()

    assert _state(feedback_id)["state"] == "delivered"
    assert _state(feedback_id)["attempts"] == 2


async def test_relaunch_drains_every_undelivered_entry(
    summarizer: FakeSummarizer,
) -> None:
    queued_down = queue.enqueue(
        **sample_enqueue_kwargs(
            session_id="sess-queued", rating="down", reasons=["slow"]
        )
    )
    summarized_down = queue.enqueue(
        **sample_enqueue_kwargs(session_id="sess-summarized", rating="down")
    )
    queue.update(
        summarized_down["id"], state="summarized", tags=["formatting"], reason="generic"
    )
    queued_up = queue.enqueue(
        **sample_enqueue_kwargs(
            session_id="sess-up", rating="up", reasons=[], comment=None
        )
    )
    delivered = queue.enqueue(
        **sample_enqueue_kwargs(
            session_id="sess-done", rating="up", reasons=[], comment=None
        )
    )
    queue.update(delivered["id"], state="delivered")
    sink = _add_sink(FakeSink())

    feedback.schedule_processing()
    await feedback.drain()

    by_id = {record["id"]: record for record in queue.read_all()}
    assert by_id[queued_down["id"]]["state"] == "delivered"
    assert by_id[queued_down["id"]]["tags"] == ["incorrect"]
    assert by_id[summarized_down["id"]]["state"] == "delivered"
    assert by_id[summarized_down["id"]]["tags"] == ["formatting"]
    assert by_id[queued_up["id"]]["state"] == "delivered"
    assert by_id[delivered["id"]]["attempts"] == 0
    assert {record["id"] for record in sink.records} == {
        queued_down["id"],
        summarized_down["id"],
        queued_up["id"],
    }
    assert summarizer.calls == [("sess-queued", ["slow"])]


async def test_concurrent_schedules_run_sink_once_per_entry(
    summarizer: FakeSummarizer,
) -> None:
    sink = _add_sink(FakeSink())
    entry = queue.enqueue(
        **sample_enqueue_kwargs(rating="up", reasons=[], comment=None)
    )

    feedback.schedule_processing()
    feedback.schedule_processing()
    await feedback.drain()

    assert len(sink.records) == 1
    assert _state(entry["id"])["attempts"] == 1


@pytest.fixture
def scope_events() -> list[str]:
    return []


def _recording_scope(events: list[str]) -> Callable[[dict[str, Any]], Any]:
    @contextlib.contextmanager
    def scope(record: dict[str, Any]) -> Iterator[None]:
        events.append("enter")
        token = TRIAGE_TAG.set("scoped")
        try:
            yield
        finally:
            TRIAGE_TAG.reset(token)
            events.append("exit")

    return scope


async def test_scope_wraps_triage_and_delivery(
    summarizer: FakeSummarizer, scope_events: list[str]
) -> None:
    seen: list[str | None] = []
    summarizer.on_call = lambda: (
        scope_events.append("summarize"),
        seen.append(TRIAGE_TAG.get()),
    )
    sink = _add_sink(FakeSink())
    sink.on_call = lambda record: (
        scope_events.append("sink"),
        seen.append(TRIAGE_TAG.get()),
    )
    feedback.register_feedback_scope(_recording_scope(scope_events))

    await _send()
    await feedback.drain()

    assert scope_events == ["enter", "summarize", "sink", "exit"]
    assert seen == ["scoped", "scoped"]
    assert TRIAGE_TAG.get() is None


async def test_scope_receives_record_copy(summarizer: FakeSummarizer) -> None:
    received: list[dict[str, Any]] = []

    def scope(record: dict[str, Any]) -> contextlib.AbstractContextManager[None]:
        received.append(record)
        return contextlib.nullcontext()

    feedback.register_feedback_scope(scope)
    feedback_id = (await _send())["feedbackId"]
    await feedback.drain()

    assert received[0]["id"] == feedback_id
    assert received[0]["comment"] == CANARY


class _EnterRaises:
    def __enter__(self) -> None:
        raise RuntimeError("enter failed")

    def __exit__(self, *exc_info: object) -> None:
        return None


@pytest.mark.parametrize("mode", ["construct", "enter"])
async def test_broken_scope_does_not_block_processing(
    summarizer: FakeSummarizer, mode: str
) -> None:
    def broken(record: dict[str, Any]) -> Any:
        if mode == "construct":
            raise RuntimeError("construct failed")
        return _EnterRaises()

    feedback.register_feedback_scope(broken)
    sink = _add_sink(FakeSink())
    feedback_id = (await _send())["feedbackId"]
    await feedback.drain()

    assert len(sink.records) == 1
    assert _state(feedback_id)["state"] == "delivered"


async def test_unregistered_scope_is_not_entered(
    summarizer: FakeSummarizer, scope_events: list[str]
) -> None:
    scope = _recording_scope(scope_events)
    feedback.register_feedback_scope(scope)
    feedback.unregister_feedback_scope(scope)

    await _send()
    await feedback.drain()

    assert scope_events == []


async def test_comment_never_reaches_debug_logs(
    summarizer: FakeSummarizer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _add_sink(FakeSink(error=RuntimeError(f"sink echoed {CANARY}")))

    await _send()
    await feedback.drain()

    assert "feedback sink failed" in caplog.text
    assert CANARY not in caplog.text


async def test_drain_without_tasks_returns_immediately() -> None:
    await asyncio.wait_for(feedback.drain(), timeout=1)


def test_schedule_processing_without_running_loop_is_noop() -> None:
    feedback.schedule_processing()
    assert feedback._tasks == set()
