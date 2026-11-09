"""ACP reply feedback: validate ``_code_puppy/feedback``, persist it, then process it in the background.

The handler returns right after the durable append; triage and delivery run in a background task
so a slow summarizer never delays the client's acknowledgement.

Called by: agent.py :: CodePuppyAgent.ext_method()/on_connect()
Depends on: feedback_queue.py, feedback_summarizer.py
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import unicodedata
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from typing import Any

from acp import RequestError

from code_puppy_core_plugins.acp import feedback_queue, feedback_summarizer
from code_puppy_core_plugins.acp.capabilities import FEEDBACK_METHOD
from code_puppy_core_plugins.acp.feedback_queue import (
    STATE_DELIVERED,
    STATE_QUEUED,
    STATE_SUMMARIZED,
)
from code_puppy_core_plugins.acp.turn_metadata import CORRELATION_ID_RE

logger = logging.getLogger(__name__)

# The SDK strips the leading "_" before routing to ext_method().
EXT_NAME = FEEDBACK_METHOD.removeprefix("_")
REASONS = ("wrong", "slow", "unsafe", "other")
COMMENT_MAX_CHARS = 254
# Memory guard: bounds the work done before sanitizing, far above the sanitized limit.
_RAW_COMMENT_MAX_CHARS = 4096
_SESSION_ID_MAX_CHARS = 256
_RATINGS = ("up", "down")
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
# A hung summarizer or sink counts as a failure so one entry cannot stall the pass.
STEP_TIMEOUT_SECONDS = 60.0
# Triage is best effort: after this many passes the vote is delivered untagged.
MAX_TRIAGE_ATTEMPTS = 3

FeedbackSink = Callable[[dict[str, Any]], Awaitable[bool]]
FeedbackScope = Callable[[dict[str, Any]], AbstractContextManager[Any]]

_sinks: list[FeedbackSink] = []
_scopes: list[FeedbackScope] = []
_hooks_lock = threading.Lock()

# Strong references keep fire-and-forget tasks alive until they finish.
_tasks: set[asyncio.Task[None]] = set()
_in_flight: set[str] = set()


def register_feedback_sink(fn: FeedbackSink) -> None:
    _register(_sinks, fn)


def unregister_feedback_sink(fn: FeedbackSink) -> None:
    _unregister(_sinks, fn)


def register_feedback_scope(fn: FeedbackScope) -> None:
    _register(_scopes, fn)


def unregister_feedback_scope(fn: FeedbackScope) -> None:
    _unregister(_scopes, fn)


def _register(registry: list[Any], fn: Any) -> None:
    with _hooks_lock:
        if fn not in registry:
            registry.append(fn)


def _unregister(registry: list[Any], fn: Any) -> None:
    with _hooks_lock:
        if fn in registry:
            registry.remove(fn)


def _snapshot(registry: list[Any]) -> list[Any]:
    with _hooks_lock:
        return list(registry)


def sanitize_comment(text: str) -> str:
    # Control and format characters become spaces so they cannot forge log or prompt structure.
    spaced = "".join(
        " " if unicodedata.category(char) in _CONTROL_CATEGORIES else char
        for char in text
    )
    return " ".join(spaced.split())


def _invalid(field: str, problem: str) -> RequestError:
    return RequestError.invalid_params({"field": field, "problem": problem})


def _session_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _SESSION_ID_MAX_CHARS
    ):
        raise _invalid("sessionId", "required non-empty string up to 256 characters")
    return value


def _correlation_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or CORRELATION_ID_RE.fullmatch(value) is None:
        raise _invalid("correlationId", "must match the correlation id format")
    return value


def _rating(value: Any) -> str:
    if value not in _RATINGS:
        raise _invalid("rating", "must be up or down")
    return value


def _reasons(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _invalid("reasons", "must be a list")
    for item in value:
        if not isinstance(item, str) or item not in REASONS:
            raise _invalid("reasons", "each reason must be one of the known values")
    if len(set(value)) != len(value):
        raise _invalid("reasons", "reasons must not repeat")
    return list(value)


def _comment(value: Any, reasons: list[str]) -> str | None:
    if value is None:
        text = None
    elif not isinstance(value, str):
        raise _invalid("comment", "must be a string")
    elif len(value) > _RAW_COMMENT_MAX_CHARS:
        raise _invalid("comment", "is too long")
    else:
        text = sanitize_comment(value)
        if len(text) > COMMENT_MAX_CHARS:
            raise _invalid("comment", "is too long after normalizing whitespace")
        text = text or None
    if "other" in reasons and text is None:
        raise _invalid("comment", "is required when reasons includes other")
    return text


def validate_params(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise _invalid("params", "must be an object")
    rating = _rating(params.get("rating"))
    if rating == "up":
        # Presence with any non-null value is rejected, even when empty.
        if params.get("reasons") is not None:
            raise _invalid("reasons", "is only allowed for rating down")
        if params.get("comment") is not None:
            raise _invalid("comment", "is only allowed for rating down")
    reasons = _reasons(params.get("reasons"))
    return {
        "session_id": _session_id(params.get("sessionId")),
        "correlation_id": _correlation_id(params.get("correlationId")),
        "rating": rating,
        "reasons": reasons,
        "comment": _comment(params.get("comment"), reasons),
    }


def _model_for(
    model_for_session: Callable[[str], str | None] | None, session_id: str
) -> str | None:
    if model_for_session is None:
        return None
    try:
        model = model_for_session(session_id)
    except Exception as exc:
        logger.debug("feedback model lookup failed: %s", type(exc).__name__)
        return None
    return model if isinstance(model, str) else None


async def handle_ext_method(
    method: str,
    params: Any,
    *,
    model_for_session: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    if method != EXT_NAME:
        raise RequestError.method_not_found("_" + method)
    fields = validate_params(params)
    model = _model_for(model_for_session, fields["session_id"])
    try:
        record = await asyncio.to_thread(feedback_queue.enqueue, model=model, **fields)
    except OSError as exc:
        logger.debug("feedback append failed: %s", type(exc).__name__)
        raise RequestError.internal_error(
            {"problem": "feedback could not be saved"}
        ) from exc
    logger.debug("feedback accepted: %s", record["id"])
    schedule_processing()
    return {"accepted": True, "feedbackId": record["id"]}


def schedule_processing() -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_process_pending())
    _tasks.add(task)
    task.add_done_callback(_on_task_done)


def _on_task_done(task: asyncio.Task[None]) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("feedback background task failed: %s", type(exc).__name__)


async def drain() -> None:
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def _process_pending() -> None:
    try:
        for record in await asyncio.to_thread(feedback_queue.pending):
            feedback_id = record["id"]
            # Check-and-add has no await between them, so it is atomic on the loop.
            if feedback_id in _in_flight:
                continue
            _in_flight.add(feedback_id)
            try:
                await process(feedback_id)
            finally:
                _in_flight.discard(feedback_id)
    except Exception as exc:
        logger.debug("feedback processing pass failed: %s", type(exc).__name__)


async def process(feedback_id: str) -> None:
    try:
        await _process(feedback_id)
    except Exception as exc:
        logger.debug("feedback processing failed: %s", type(exc).__name__)


async def _process(feedback_id: str) -> None:
    record = await asyncio.to_thread(feedback_queue.get, feedback_id)
    if record is None or record.get("state") == STATE_DELIVERED:
        return
    await asyncio.to_thread(feedback_queue.bump_attempts, feedback_id)
    record = await asyncio.to_thread(feedback_queue.get, feedback_id)
    if record is None:
        return
    # Scopes wrap triage and delivery, so context set on enter reaches both.
    with contextlib.ExitStack() as stack:
        _enter_scopes(stack, record)
        triaged = await _summarize_if_needed(record)
        if triaged is not None:
            await _deliver(triaged)


def _enter_scopes(stack: contextlib.ExitStack, record: dict[str, Any]) -> None:
    for scope in _snapshot(_scopes):
        try:
            stack.enter_context(scope(dict(record)))
        except Exception as exc:
            logger.debug("feedback scope failed: %s", type(exc).__name__)


async def _summarize_if_needed(record: dict[str, Any]) -> dict[str, Any] | None:
    if record.get("rating") != "down" or record.get("state") != STATE_QUEUED:
        return record
    summary = await _triage(record)
    if summary is None:
        if record.get("attempts", 0) < MAX_TRIAGE_ATTEMPTS:
            return None
        summary = feedback_summarizer.FeedbackSummary(tags=[], reason=None)
    return await asyncio.to_thread(
        feedback_queue.update,
        record["id"],
        state=STATE_SUMMARIZED,
        tags=summary.tags,
        reason=summary.reason,
    )


async def _triage(record: dict[str, Any]) -> feedback_summarizer.FeedbackSummary | None:
    try:
        return await asyncio.wait_for(
            feedback_summarizer.summarize(
                record["sessionId"], record.get("reasons") or []
            ),
            STEP_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.debug("feedback triage failed: %s", type(exc).__name__)
        return None


async def _deliver(record: dict[str, Any]) -> None:
    all_ok = True
    for sink in _snapshot(_sinks):
        if not await _call_sink(sink, record):
            all_ok = False
    if all_ok:
        await asyncio.to_thread(
            feedback_queue.update, record["id"], state=STATE_DELIVERED
        )


async def _call_sink(sink: FeedbackSink, record: dict[str, Any]) -> bool:
    try:
        result = await asyncio.wait_for(sink(dict(record)), STEP_TIMEOUT_SECONDS)
        return result is True
    except Exception as exc:
        logger.debug("feedback sink failed: %s", type(exc).__name__)
        return False
