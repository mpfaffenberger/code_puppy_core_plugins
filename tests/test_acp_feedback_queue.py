"""Tests for the durable ACP feedback queue (feedback.jsonl)."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import code_puppy.config as config
from code_puppy_core_plugins.acp import feedback_queue as queue
from tests.acp_feedback_support import feedback_data_dir  # noqa: F401
from tests.acp_feedback_support import read_lines, sample_enqueue_kwargs

pytestmark = pytest.mark.usefixtures("feedback_data_dir")

TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
RECORD_KEYS = {
    "id",
    "createdAt",
    "state",
    "attempts",
    "sessionId",
    "correlationId",
    "rating",
    "reasons",
    "comment",
    "model",
    "tags",
    "reason",
}


@pytest.fixture
def data_dir(request: pytest.FixtureRequest) -> Path:
    return request.getfixturevalue("feedback_data_dir")


def _feedback_file(data_dir: Path) -> Path:
    return data_dir / "feedback.jsonl"


def _enqueue_many(count: int) -> list[dict[str, Any]]:
    return [queue.enqueue(**sample_enqueue_kwargs()) for _ in range(count)]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, {"correlationId": "corr-1", "comment": "bad answer", "model": "model-x"}),
        (
            {"correlation_id": None, "comment": None, "model": None},
            {"correlationId": None, "comment": None, "model": None},
        ),
    ],
    ids=["all-set", "optional-none"],
)
def test_enqueue_appends_one_line_with_contract_fields(
    data_dir: Path, overrides: dict[str, Any], expected: dict[str, Any]
) -> None:
    record = queue.enqueue(**sample_enqueue_kwargs(**overrides))

    path = _feedback_file(data_dir)
    lines = read_lines(path)
    assert len(lines) == 1
    assert json.loads(lines[0]) == record
    assert set(record) == RECORD_KEYS
    assert uuid.UUID(record["id"]).version == 4
    assert TIMESTAMP_RE.fullmatch(record["createdAt"])
    assert record["state"] == queue.STATE_QUEUED
    assert record["attempts"] == 0
    assert record["tags"] == []
    assert record["reason"] is None
    assert record["sessionId"] == "sess_test"
    assert record["rating"] == "down"
    assert record["reasons"] == ["wrong"]
    assert {key: record[key] for key in expected} == expected


def test_enqueue_creates_missing_data_dir(data_dir: Path) -> None:
    assert not data_dir.exists()

    queue.enqueue(**sample_enqueue_kwargs())

    assert _feedback_file(data_dir).is_file()


def test_path_follows_data_dir_at_call_time(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue.enqueue(**sample_enqueue_kwargs())
    other_dir = tmp_path / "other"
    monkeypatch.setattr(config, "DATA_DIR", str(other_dir))

    queue.enqueue(**sample_enqueue_kwargs())

    assert len(read_lines(_feedback_file(data_dir))) == 1
    assert len(read_lines(_feedback_file(other_dir))) == 1


def test_enqueue_fsyncs_after_write_and_before_return(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _feedback_file(data_dir)
    real_fsync = os.fsync
    events: list[bool] = []

    def spy_fsync(fd: int) -> None:
        # The record must already be written (newline-terminated) at fsync time.
        events.append(path.read_bytes().endswith(b"\n"))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)

    record = queue.enqueue(**sample_enqueue_kwargs())

    assert events == [True]
    assert json.loads(read_lines(path)[0]) == record


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param(lambda raw: raw + b'{"id":"torn"', id="partial-record"),
        pytest.param(lambda raw: raw.rstrip(b"\n"), id="complete-record-no-newline"),
    ],
)
def test_torn_tail_does_not_corrupt_next_record(
    data_dir: Path, tail: Callable[[bytes], bytes]
) -> None:
    first = queue.enqueue(**sample_enqueue_kwargs())
    path = _feedback_file(data_dir)
    path.write_bytes(tail(path.read_bytes()))

    second = queue.enqueue(**sample_enqueue_kwargs())

    assert [record["id"] for record in queue.read_all()] == [first["id"], second["id"]]
    assert json.loads(read_lines(path)[-1]) == second


@pytest.mark.parametrize(
    "junk",
    [
        b"",
        b"   ",
        b"{not json",
        b"[1, 2]",
        b'"text"',
        b"null",
        b'{"comment": "no id"}',
        b'{"id": 7}',
        b"\xff\xfe",
    ],
)
def test_read_all_skips_unusable_lines(data_dir: Path, junk: bytes) -> None:
    first = queue.enqueue(**sample_enqueue_kwargs())
    path = _feedback_file(data_dir)
    second_line = json.dumps(first | {"id": "second"}).encode()
    path.write_bytes(path.read_bytes() + junk + b"\n" + second_line + b"\n")

    assert [record["id"] for record in queue.read_all()] == [first["id"], "second"]


def test_read_all_returns_empty_when_file_missing(data_dir: Path) -> None:
    assert queue.read_all() == []
    assert queue.pending() == []


def test_pending_excludes_delivered_and_keeps_file_order(
    data_dir: Path,
) -> None:
    first, second, third = _enqueue_many(3)
    queue.update(second["id"], state=queue.STATE_DELIVERED)
    queue.update(third["id"], state=queue.STATE_SUMMARIZED)

    assert [record["id"] for record in queue.pending()] == [first["id"], third["id"]]


def test_get_returns_record_or_none(data_dir: Path) -> None:
    record = queue.enqueue(**sample_enqueue_kwargs())

    assert queue.get(record["id"]) == record
    assert queue.get(str(uuid.uuid4())) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", queue.STATE_SUMMARIZED),
        ("attempts", 3),
        ("tags", ["dup", "spam"]),
        ("reason", "duplicate"),
        ("reason", None),
        ("model", "model-y"),
    ],
)
def test_update_persists_allowed_fields(data_dir: Path, field: str, value: Any) -> None:
    record = queue.enqueue(**sample_enqueue_kwargs())

    updated = queue.update(record["id"], **{field: value})

    assert updated is not None
    assert updated[field] == value
    assert queue.get(record["id"])[field] == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sessionId", "other"),
        ("id", "forged"),
        ("rating", "up"),
        ("state", "bogus"),
        ("attempts", -1),
        ("attempts", True),
        ("tags", "not-a-list"),
        ("tags", [1]),
        ("reason", 5),
        ("model", 5),
    ],
)
def test_update_rejects_unknown_or_invalid_fields(
    data_dir: Path, field: str, value: Any
) -> None:
    record = queue.enqueue(**sample_enqueue_kwargs())
    path = _feedback_file(data_dir)
    before = path.read_bytes()

    with pytest.raises(ValueError):
        queue.update(record["id"], **{field: value})

    assert path.read_bytes() == before


def test_missing_id_returns_none_and_leaves_bytes_unchanged(
    data_dir: Path,
) -> None:
    queue.enqueue(**sample_enqueue_kwargs())
    path = _feedback_file(data_dir)
    before = path.read_bytes()
    unknown = str(uuid.uuid4())

    assert queue.update(unknown, state=queue.STATE_DELIVERED) is None
    assert queue.bump_attempts(unknown) is None

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda feedback_id: queue.update(feedback_id, state=queue.STATE_DELIVERED),
            id="update",
        ),
        pytest.param(queue.bump_attempts, id="bump_attempts"),
    ],
)
def test_mutation_on_missing_file_returns_none_and_creates_nothing(
    data_dir: Path,
    mutate: Callable[[str], dict[str, Any] | None],
) -> None:
    assert mutate(str(uuid.uuid4())) is None

    assert not _feedback_file(data_dir).exists()


def test_update_preserves_other_and_malformed_lines_verbatim(
    data_dir: Path,
) -> None:
    first, _second = _enqueue_many(2)
    path = _feedback_file(data_dir)
    path.write_bytes(path.read_bytes() + b"{not json\n\n[1, 2]\n")
    before = read_lines(path)

    queue.update(first["id"], state=queue.STATE_DELIVERED)

    after = read_lines(path)
    assert len(after) == len(before)
    assert json.loads(after[0]) == first | {"state": queue.STATE_DELIVERED}
    assert after[1:] == before[1:]


def test_update_rewrite_failure_keeps_original_file(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = queue.enqueue(**sample_enqueue_kwargs())
    path = _feedback_file(data_dir)
    before = path.read_bytes()

    def failing_write(target: str, data: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(queue, "atomic_write_bytes", failing_write)

    with pytest.raises(OSError, match="disk full"):
        queue.update(record["id"], state=queue.STATE_DELIVERED)

    assert path.read_bytes() == before


def test_bump_attempts_increments_and_persists(data_dir: Path) -> None:
    record = queue.enqueue(**sample_enqueue_kwargs())

    queue.bump_attempts(record["id"])
    bumped = queue.bump_attempts(record["id"])

    assert bumped is not None and bumped["attempts"] == 2
    assert queue.get(record["id"])["attempts"] == 2


def test_concurrent_bump_loses_no_increments(data_dir: Path) -> None:
    record = queue.enqueue(**sample_enqueue_kwargs())

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: queue.bump_attempts(record["id"]), range(20)))

    assert sorted(result["attempts"] for result in results) == list(range(1, 21))
    assert queue.get(record["id"])["attempts"] == 20
    for line in read_lines(_feedback_file(data_dir)):
        json.loads(line)


def test_concurrent_enqueue_writes_distinct_valid_lines(
    data_dir: Path,
) -> None:
    with ThreadPoolExecutor(max_workers=20) as pool:
        records = list(
            pool.map(
                lambda _: queue.enqueue(**sample_enqueue_kwargs()),
                range(20),
            )
        )

    lines = read_lines(_feedback_file(data_dir))
    parsed = [json.loads(line) for line in lines]
    assert len(lines) == 20
    assert {record["id"] for record in parsed} == {record["id"] for record in records}


def test_debug_logs_never_include_record_contents(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "SENSITIVE-COMMENT-TEXT"
    queue.enqueue(**sample_enqueue_kwargs(comment=secret))
    path = _feedback_file(data_dir)
    path.write_bytes(
        path.read_bytes() + f'{{"id": "x", "comment": "{secret}"\n'.encode()
    )

    with caplog.at_level(logging.DEBUG):
        assert len(queue.read_all()) == 1

    assert "malformed" in caplog.text
    assert secret not in caplog.text
