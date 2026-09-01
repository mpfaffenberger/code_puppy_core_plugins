"""Tests for the emit-hook seam that carries ``is_fork`` from the bus message
into ``state.register`` (``_install_emit_hook`` / ``MessageBus.emit`` patch).

``test_panel_lines.py`` covers rendering by calling ``state.register(...)``
directly -- it never exercises the hand-off itself. These tests drive the
*actual* patched ``MessageBus.emit`` with synthetic ``SubAgentInvocationMessage``
-shaped objects (matched by ``type(message).__name__``, exactly like the real
hook dispatches), proving the ``getattr(message, "is_fork", False)``
graceful-degradation path works end to end, not just in isolation.

Deliberately synthetic rather than the real pydantic ``SubAgentInvocationMessage``:
that model sets ``extra: "forbid"``, so constructing it with ``is_fork=`` fails
against whichever core happens to be installed in the test environment if that
core predates the field (mirrors the fake ``impl_with_is_fork`` /
``impl_without_is_fork`` pattern in ``test_fork_plugin.py`` for the same reason
-- these tests must pass regardless of installed core version).
"""

from __future__ import annotations

import pytest

from code_puppy.messaging.bus import MessageBus
from code_puppy_core_plugins.subagent_panel import register_callbacks as rc
from code_puppy_core_plugins.subagent_panel import state


@pytest.fixture(autouse=True)
def clean_state():
    state.clear()
    yield
    state.clear()


@pytest.fixture
def bus():
    """A real MessageBus instance is a safe ``self`` for the patched emit --
    no handlers are registered, so the underlying call is a cheap no-op."""
    return MessageBus()


def _fake_invocation_message(session_id, agent_name="code-puppy", **extra):
    """Build an object matching ``type(message).__name__ ==
    'SubAgentInvocationMessage'`` (the hook's dispatch key) without going
    through the real pydantic model, so these tests are independent of
    whichever core version happens to be installed."""
    Message = type("SubAgentInvocationMessage", (), {})
    msg = Message()
    msg.session_id = session_id
    msg.agent_name = agent_name
    msg.model_name = None
    for key, value in extra.items():
        setattr(msg, key, value)
    return msg


def _emit(bus, message) -> None:
    rc._install_emit_hook()  # idempotent; ensures MessageBus.emit is patched
    MessageBus.emit(bus, message)


def test_emit_hook_registers_is_fork_true(bus):
    msg = _fake_invocation_message("sess-fork-1", is_fork=True)
    _emit(bus, msg)
    entry = state.snapshot()[0]
    assert entry["session_id"] == "sess-fork-1"
    assert entry["is_fork"] is True


def test_emit_hook_registers_is_fork_false(bus):
    msg = _fake_invocation_message("sess-2", is_fork=False)
    _emit(bus, msg)
    entry = state.snapshot()[0]
    assert entry["is_fork"] is False


def test_emit_hook_defaults_is_fork_false_for_old_core_message_shape(bus):
    """Simulates an OLD core whose ``SubAgentInvocationMessage`` predates the
    ``is_fork`` field entirely (attribute absent, not just ``False``)."""
    msg = _fake_invocation_message("sess-old-core")  # no is_fork kwarg at all
    _emit(bus, msg)
    entry = state.snapshot()[0]
    assert entry["session_id"] == "sess-old-core"
    assert entry["is_fork"] is False
