"""Shared fixtures and helpers for the ACP feedback queue tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import code_puppy.config as config
from code_puppy_core_plugins.acp import feedback


@pytest.fixture
def feedback_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    return data_dir


def sample_enqueue_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "session_id": "sess_test",
        "correlation_id": "corr-1",
        "rating": "down",
        "reasons": ["wrong"],
        "comment": "bad answer",
        "model": "model-x",
    }
    kwargs.update(overrides)
    return kwargs


def read_lines(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


@pytest.fixture
def clean_feedback_state() -> Iterator[None]:
    # Hooks and background tasks are module globals; restore them so tests stay isolated.
    sinks, scopes = list(feedback._sinks), list(feedback._scopes)
    feedback._tasks.clear()
    feedback._in_flight.clear()
    yield
    feedback._sinks[:] = sinks
    feedback._scopes[:] = scopes
    feedback._tasks.clear()
    feedback._in_flight.clear()
