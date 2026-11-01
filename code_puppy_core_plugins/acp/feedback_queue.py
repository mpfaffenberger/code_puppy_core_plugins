"""Durable queue for ACP reply feedback, one JSON object per line in ``feedback.jsonl``.

Called by: feedback.py :: handle_ext_method()/process()
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import code_puppy.config as config
from code_puppy.atomic_io import (
    ContentTooLarge,
    atomic_write_bytes,
    path_lock,
    read_bounded_bytes,
)

logger = logging.getLogger(__name__)

STATE_QUEUED = "queued"
STATE_SUMMARIZED = "summarized"
STATE_DELIVERED = "delivered"
_STATES = frozenset({STATE_QUEUED, STATE_SUMMARIZED, STATE_DELIVERED})
_MUTABLE_FIELDS = frozenset({"state", "attempts", "tags", "reason", "model"})

# Bounds the read so a runaway file cannot exhaust memory. Oversized files make
# mutations raise instead of rewriting from a truncated view.
_MAX_QUEUE_BYTES = 64 * 1024 * 1024

# path_lock is neither reentrant nor thread-safe within one process, so threads
# take this lock first; every mutation uses the same order to avoid deadlock.
_thread_lock = threading.Lock()


def feedback_path() -> Path:
    # Resolved per call so tests can redirect code_puppy.config.DATA_DIR.
    return Path(config.DATA_DIR) / "feedback.jsonl"


def enqueue(
    *,
    session_id: str,
    correlation_id: str | None,
    rating: str,
    reasons: list[str],
    comment: str | None,
    model: str | None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "createdAt": _utc_now_iso(),
        "state": STATE_QUEUED,
        "attempts": 0,
        "sessionId": session_id,
        "correlationId": correlation_id,
        "rating": rating,
        "reasons": list(reasons),
        "comment": comment,
        "model": model,
        "tags": [],
        "reason": None,
    }
    line = _encode(record) + b"\n"
    path = feedback_path()
    with _thread_lock, path_lock(str(path)):
        path.parent.mkdir(parents=True, exist_ok=True)
        # A torn previous append must not swallow this record into its line.
        prefix = b"\n" if _ends_without_newline(path) else b""
        with open(path, "ab") as handle:
            handle.write(prefix + line)
            handle.flush()
            os.fsync(handle.fileno())
    return dict(record)


def read_all() -> list[dict[str, Any]]:
    try:
        raw = read_bounded_bytes(str(feedback_path()), _MAX_QUEUE_BYTES)
    except (OSError, ContentTooLarge) as exc:
        logger.debug("Feedback queue unreadable (%s)", type(exc).__name__)
        return []
    records = []
    for line_no, line in enumerate(_split_lines(raw), start=1):
        record = _parse_record(line, line_no)
        if record is not None:
            records.append(record)
    return records


def get(feedback_id: str) -> dict[str, Any] | None:
    for record in read_all():
        if record["id"] == feedback_id:
            return record
    return None


def pending() -> list[dict[str, Any]]:
    return [record for record in read_all() if record.get("state") != STATE_DELIVERED]


def update(feedback_id: str, **fields: Any) -> dict[str, Any] | None:
    _validate_fields(fields)
    return _locked_rewrite(feedback_id, lambda record: {**record, **fields})


def bump_attempts(feedback_id: str) -> dict[str, Any] | None:
    return _locked_rewrite(feedback_id, _increment_attempts)


def _increment_attempts(record: dict[str, Any]) -> dict[str, Any]:
    current = record.get("attempts")
    record["attempts"] = (current if isinstance(current, int) else 0) + 1
    return record


def _locked_rewrite(
    feedback_id: str, mutate: Callable[[dict[str, Any]], dict[str, Any]]
) -> dict[str, Any] | None:
    path = feedback_path()
    with _thread_lock, path_lock(str(path)):
        try:
            raw = read_bounded_bytes(str(path), _MAX_QUEUE_BYTES)
        except FileNotFoundError:
            # Mutations never create the queue file.
            return None
        lines = _split_lines(raw)
        for index, line in enumerate(lines):
            record = _parse_record(line, index + 1)
            if record is None or record["id"] != feedback_id:
                continue
            updated = mutate(record)
            # Untouched lines (including malformed ones) are written back verbatim.
            lines[index] = _encode(updated)
            atomic_write_bytes(str(path), b"".join(item + b"\n" for item in lines))
            return updated
    return None


def _validate_fields(fields: dict[str, Any]) -> None:
    unknown = set(fields) - _MUTABLE_FIELDS
    if unknown:
        raise ValueError(f"Unsupported feedback field(s): {sorted(unknown)}")
    if "state" in fields and not _is_state(fields["state"]):
        raise ValueError(f"Invalid feedback state: {fields['state']!r}")
    if "attempts" in fields and not _is_count(fields["attempts"]):
        raise ValueError("attempts must be a non-negative int")
    if "tags" in fields and not _is_str_list(fields["tags"]):
        raise ValueError("tags must be a list of str")
    if "reason" in fields and not _is_optional_str(fields["reason"]):
        raise ValueError("reason must be str or None")
    if "model" in fields and not _is_optional_str(fields["model"]):
        raise ValueError("model must be str or None")


def _is_state(value: Any) -> bool:
    return isinstance(value, str) and value in _STATES


def _is_count(value: Any) -> bool:
    # bool is a subclass of int; reject it explicitly.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_optional_str(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _parse_record(line: bytes, line_no: int) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        value = json.loads(line)
    except ValueError as exc:
        logger.debug(
            "Skipping malformed feedback line %d (%s)", line_no, type(exc).__name__
        )
        return None
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        logger.debug("Skipping feedback line %d: not a feedback record", line_no)
        return None
    return value


def _encode(record: dict[str, Any]) -> bytes:
    # ASCII-escaped output keeps every record on one line and avoids
    # encode errors from lone surrogates in user comments.
    return json.dumps(record, separators=(",", ":")).encode("ascii")


def _split_lines(raw: bytes) -> list[bytes]:
    lines = raw.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    return lines


def _ends_without_newline(path: Path) -> bool:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size == 0:
        return False
    with open(path, "rb") as handle:
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) != b"\n"


def _utc_now_iso() -> str:
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")
