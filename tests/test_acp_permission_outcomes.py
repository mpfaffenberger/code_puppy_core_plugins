"""ACP permission outcomes: only a person's Reject is reported as a rejection.

A cancelled, failed or timed-out permission request still denies the
operation (fail closed), but it is reported as ``PermissionNotDecided`` so the
model is not told the user rejected something they never saw or answered.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

from code_puppy_core_plugins.acp import permissions, state


class _Connection:
    def __init__(self, outcome: Any = None, *, error: Exception | None = None):
        self._outcome = outcome
        self._error = error
        self.requests = 0

    async def request_permission(self, options, session_id, tool_call):
        self.requests += 1
        if self._error is not None:
            raise self._error
        if self._outcome is None:
            await asyncio.Event().wait()  # never answered
        return RequestPermissionResponse(outcome=self._outcome)


def _selected(option_id: str) -> AllowedOutcome:
    return AllowedOutcome(option_id=option_id, outcome="selected")


@pytest.fixture
def connect():
    def _connect(conn: Any) -> None:
        state.set_connection(conn, asyncio.get_running_loop())
        state.begin_run("s1")

    yield _connect
    state.end_run()
    state.set_connection(None, None)


@pytest.mark.asyncio
async def test_allow_and_reject_are_decisions(connect):
    connect(_Connection(_selected("allow_once")))
    assert await permissions._ask_client_outcome("s1", "t") == (True, None)

    connect(_Connection(_selected("reject_once")))
    assert await permissions._ask_client_outcome("s1", "t") == (False, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conn",
    [
        _Connection(DeniedOutcome(outcome="cancelled")),
        _Connection(_selected("something_new")),
        _Connection(error=RuntimeError("transport closed")),
    ],
    ids=["cancelled", "unknown-option", "request-error"],
)
async def test_denials_nobody_chose_are_not_decided(connect, conn):
    connect(conn)

    allowed, not_decided = await permissions._ask_client_outcome("s1", "t")

    assert allowed is False
    assert isinstance(not_decided, permissions.PermissionNotDecided)


@pytest.mark.asyncio
async def test_unanswered_request_times_out_as_not_decided(connect, monkeypatch):
    monkeypatch.setattr(permissions, "_PERMISSION_TIMEOUT_S", 0.01)
    connect(_Connection(None))

    allowed, not_decided = await permissions._ask_client_outcome("s1", "t")

    assert allowed is False
    assert "timed out" in not_decided


@pytest.mark.asyncio
async def test_no_connection_is_not_decided():
    allowed, not_decided = await permissions._ask_client_outcome("s1", "t")

    assert allowed is False
    assert isinstance(not_decided, permissions.PermissionNotDecided)


@pytest.mark.asyncio
async def test_ask_client_keeps_its_boolean_contract(connect):
    connect(_Connection(DeniedOutcome(outcome="cancelled")))

    assert await permissions._ask_client("s1", "t") is False


@pytest.mark.asyncio
async def test_file_backend_passes_not_decided_through_as_feedback(connect):
    connect(_Connection(DeniedOutcome(outcome="cancelled")))

    approved, feedback = await asyncio.to_thread(
        permissions._approval_backend, "Edit notes.txt", "msg", None
    )

    assert approved is False
    assert isinstance(feedback, permissions.PermissionNotDecided)


@pytest.mark.asyncio
async def test_file_backend_reject_has_no_feedback(connect):
    connect(_Connection(_selected("reject_once")))

    result = await asyncio.to_thread(
        permissions._approval_backend, "Edit notes.txt", "msg", None
    )

    assert result == (False, None)


def test_file_backend_without_a_client_is_not_decided():
    approved, feedback = permissions._approval_backend("Edit", "msg", None)

    assert approved is False
    assert isinstance(feedback, permissions.PermissionNotDecided)


@pytest.mark.asyncio
async def test_shell_hook_distinguishes_reject_from_no_decision(connect, monkeypatch):
    monkeypatch.setattr("code_puppy.config.get_yolo_mode", lambda: False)

    connect(_Connection(_selected("reject_once")))
    rejected = await permissions._on_run_shell_command(None, "rm x", None, 60)
    assert rejected["blocked"] is True
    assert rejected["error_message"] == "Command rejected in the client"

    connect(_Connection(DeniedOutcome(outcome="cancelled")))
    undecided = await permissions._on_run_shell_command(None, "rm x", None, 60)
    assert undecided["blocked"] is True
    assert undecided["error_message"] == "Permission not granted"
    assert "did not reject" in undecided["reasoning"]


@pytest.mark.asyncio
async def test_shell_hook_fails_closed_when_the_connection_is_gone(monkeypatch):
    """Inside an ACP run with no client to ask, the command must not run."""
    monkeypatch.setattr("code_puppy.config.get_yolo_mode", lambda: False)
    state.begin_run("s1")
    try:
        result = await permissions._on_run_shell_command(None, "rm x", None, 60)
    finally:
        state.end_run()

    assert result is not None
    assert result["blocked"] is True


@pytest.mark.asyncio
async def test_shell_hook_is_inert_outside_an_acp_run():
    assert await permissions._on_run_shell_command(None, "ls", None, 60) is None
