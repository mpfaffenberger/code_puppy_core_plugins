"""ACP approval parity: one shell gate in core, no ACP-only timeout, cancel/close deny.

On cores that ship ``can_prompt_for_approval``, shell approval runs through core's
gate and the ACP hook is not registered. Older cores get the legacy hook only.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, List

import pytest
from acp.schema import AllowedOutcome, RequestPermissionResponse

from code_puppy.callbacks import get_callbacks
from code_puppy.tools import common
from code_puppy_core_plugins.acp import permissions, state


class _Dialog:
    """A client permission dialog that stays open until the test answers it."""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.answer = asyncio.Event()
        self.outcome: Any = None

    async def request_permission(self, options, session_id, tool_call):
        if self.error is not None:
            raise self.error
        await self.answer.wait()
        return RequestPermissionResponse(outcome=self.outcome)


@pytest.fixture
def connect():
    def _connect(conn: Any) -> None:
        state.set_connection(conn, asyncio.get_running_loop())
        state.begin_run("s1")

    yield _connect
    state.end_run()
    state.set_connection(None, None)


@pytest.fixture
def installed():
    yield
    permissions.uninstall()


async def _wait_until_pending(session_id: str = "s1") -> None:
    for _ in range(500):
        with permissions._PENDING_LOCK:
            if permissions._PENDING.get(session_id):
                return
        await asyncio.sleep(0.01)
    raise AssertionError("permission request never became pending")


def _legacy_hooks() -> List[Any]:
    return [
        cb
        for cb in get_callbacks("run_shell_command", include_disabled=True)
        if cb is permissions._legacy_shell_hook
    ]


def _is_not_decided(feedback: Any) -> bool:
    return isinstance(feedback, permissions.PermissionNotDecided)


async def test_no_acp_permission_timeout_remains():
    assert not hasattr(permissions, "_PERMISSION_TIMEOUT_S")
    assert "wait_for" not in inspect.getsource(permissions)


@pytest.mark.asyncio
async def test_pending_request_is_not_timed_out_after_600_seconds(connect, monkeypatch):
    dialog = _Dialog()
    connect(dialog)
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(permissions._ask_client_outcome("s1", "t"))
    await asyncio.sleep(0)

    # Jump the loop clock past the old 600 s deadline; a wait_for timer would fire.
    now = loop.time()
    monkeypatch.setattr(loop, "time", lambda: now + 601)
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()

    dialog.outcome = AllowedOutcome(option_id="allow_once", outcome="selected")
    dialog.answer.set()
    assert await task == (True, None)


@pytest.mark.asyncio
async def test_cancel_pending_denies_and_unblocks_the_tool_thread(connect):
    connect(_Dialog())
    worker = asyncio.ensure_future(
        asyncio.to_thread(permissions._approval_backend, "Edit", "msg", None)
    )
    await _wait_until_pending()

    permissions.cancel_pending("s1")

    approved, feedback = await asyncio.wait_for(worker, timeout=5)
    assert approved is False
    assert _is_not_decided(feedback)
    assert "cancelled" in feedback
    assert not permissions._PENDING


@pytest.mark.asyncio
async def test_cancel_pending_is_scoped_to_one_session(connect):
    connect(_Dialog())
    worker = asyncio.ensure_future(
        asyncio.to_thread(permissions._approval_backend, "Edit", "msg", None)
    )
    await _wait_until_pending()

    permissions.cancel_pending("some-other-session")
    await asyncio.sleep(0.05)
    assert not worker.done()

    permissions.cancel_pending()
    approved, feedback = await asyncio.wait_for(worker, timeout=5)
    assert approved is False
    assert _is_not_decided(feedback)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [ConnectionResetError("client disconnected"), RuntimeError("transport closed")],
    ids=["reset", "closed"],
)
async def test_closed_or_failed_connection_denies_as_not_decided(connect, error):
    connect(_Dialog(error=error))

    approved, feedback = await asyncio.to_thread(
        permissions._approval_backend, "Edit", "msg", None
    )

    assert approved is False
    assert _is_not_decided(feedback)
    assert not permissions._PENDING


@pytest.fixture
def _legacy_core(monkeypatch):
    monkeypatch.delattr(common, "can_prompt_for_approval", raising=False)


@pytest.fixture
def _current_core(monkeypatch):
    monkeypatch.setattr(common, "can_prompt_for_approval", lambda: True, raising=False)


def test_legacy_hook_registered_only_when_core_lacks_the_gate(_legacy_core, installed):
    permissions.install()
    assert _legacy_hooks() == [permissions._legacy_shell_hook]

    permissions.uninstall()
    assert _legacy_hooks() == []


def test_install_registers_no_shell_hook_when_core_has_the_gate(
    _current_core, installed
):
    permissions.install()

    assert _legacy_hooks() == []
    assert common.get_approval_backend() is permissions._approval_backend


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("option_id", "expected"),
    [("allow_once", (True, None)), ("reject_once", (False, None))],
    ids=["allow", "reject"],
)
async def test_backend_maps_allow_and_reject_unchanged(connect, option_id, expected):
    dialog = _Dialog()
    dialog.outcome = AllowedOutcome(option_id=option_id, outcome="selected")
    dialog.answer.set()
    connect(dialog)

    result = await asyncio.to_thread(permissions._approval_backend, "Edit", "msg", None)

    assert result == expected
    assert not permissions._PENDING
