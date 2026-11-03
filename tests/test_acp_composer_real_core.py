"""ACP composer wiring against the REAL new core APIs (no fakes of them).

The whole module is skipped on the old core, where these APIs do not exist.
Only the agent run is faked; the pause controllers, interrupted-subagent store
and command catalog are the real core objects.
"""

from __future__ import annotations

import asyncio
import importlib
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

pytest.importorskip("code_puppy.command_line.command_catalog")
pytest.importorskip("code_puppy.model_media")
pause = pytest.importorskip("code_puppy.messaging.pause_controller")
# Circular-import guard: subagent_invocation must load after agent_tools.
importlib.import_module("code_puppy.tools.agent_tools")
subagents = pytest.importorskip("code_puppy.tools.subagent_invocation")
if not (
    hasattr(pause, "get_session_pause_controller")
    and hasattr(subagents, "peek_interrupted_subagents")
):
    pytest.skip("new core APIs absent", allow_module_level=True)

import code_puppy.config as config  # noqa: E402
from code_puppy.tools.subagent_context import (  # noqa: E402
    reset_conversation_root_id,
    set_conversation_root_id,
)
from code_puppy_core_plugins.acp import composer_ext, persistence  # noqa: E402
from tests.acp_session_support import (  # noqa: E402
    FakeAgent,
    make_session,
    running_turn,
    text_block,
)

SESSION_A = "sess-real-a"
SESSION_B = "sess-real-b"
RECORD_A = {"agent_name": "retriever", "session_id": "sub-a", "saved_count": 3}
RECORD_B = {"agent_name": "terrier", "session_id": "sub-b", "saved_count": None}
COMMAND_KEY = "codePuppy/command"


@pytest.fixture
def real_state(monkeypatch) -> Iterator[SimpleNamespace]:
    """Stub disk writes and drop every real controller/record the test registered."""
    real = SimpleNamespace(touched=[], saved_history=[])
    monkeypatch.setattr(config, "save_command_to_history", real.saved_history.append)
    monkeypatch.setattr(persistence, "save", lambda *args, **kwargs: None)
    yield real
    for session_id in real.touched:
        pause.drop_session_pause_controller(session_id)
        subagents.clear_interrupted_subagents(session_id)


@contextmanager
def conversation_root(root_id: str) -> Iterator[None]:
    token = set_conversation_root_id(root_id)
    try:
        yield
    finally:
        reset_conversation_root_id(token)


def record_interrupted(root_id: str, record: dict[str, Any]) -> None:
    with conversation_root(root_id):
        subagents.record_interrupted_subagent(
            agent_name=record["agent_name"],
            session_id=record["session_id"],
            saved_count=record["saved_count"],
        )


def _pending_now(session_id: str) -> list[str]:
    """Return the now-mode steer queue, restoring it; the real controller has no peek for it."""
    controller = pause.get_session_pause_controller(session_id)
    drained = controller.drain_pending_steer_now()
    for text in drained:
        controller.request_steer(text, "now", history_recorded=True)
    return drained


# ---- Available commands (real catalog) ------------------------------------
@pytest.fixture(scope="module")
def catalog() -> list[Any]:
    # Cold plugin discovery takes ~50s; one module-wide load serves the single test.
    return composer_ext.available_commands()


def test_real_command_catalog_shape(catalog):
    by_name = {command.name: command for command in catalog}

    assert len(catalog) > 4  # the old ACP list exposed four commands
    assert all(not command.name.startswith("/") for command in catalog)
    for command in catalog:
        assert set(command.field_meta[COMMAND_KEY]) == {
            "interactive",
            "aliases",
            "category",
        }
    assert by_name["model"].field_meta[COMMAND_KEY]["interactive"] is True
    assert by_name["agent"].field_meta[COMMAND_KEY]["interactive"] is True
    assert by_name["help"].field_meta[COMMAND_KEY]["interactive"] is False

    wire = by_name["model"].model_dump(by_alias=True, exclude_none=True, mode="json")
    assert set(wire["input"]) == {"hint"}
    assert wire["input"]["hint"]


# ---- Steering with real per-session controllers ---------------------------
async def test_steer_queues_only_on_the_steered_session(real_state):
    real_state.touched += [SESSION_A, SESSION_B]
    session_a = make_session(SESSION_A, FakeAgent(script=["block"]))
    session_b = make_session(SESSION_B, FakeAgent(script=["block", "block"]))

    async with running_turn(session_a):
        async with running_turn(session_b):
            assert session_a.steer([text_block("nudge A")]) == {"accepted": True}
            assert _pending_now(SESSION_A) == ["nudge A"]
            assert _pending_now(SESSION_B) == []
            assert pause.get_pause_controller().pending_steer_counts() == (0, 0)

        # B has finished; a new B run must leave A's steer untouched.
        async with running_turn(session_b):
            assert _pending_now(SESSION_A) == ["nudge A"]
            assert _pending_now(SESSION_B) == []
            assert session_b.steer([text_block("nudge B")]) == {"accepted": True}
            assert _pending_now(SESSION_B) == ["nudge B"]
            assert _pending_now(SESSION_A) == ["nudge A"]
            assert pause.get_pause_controller().pending_steer_counts() == (0, 0)

    # ACP steers are editor-owned: none may reach the terminal history file.
    assert real_state.saved_history == []


# ---- Interrupted-subagent records (real store) ----------------------------
async def test_cancelled_turn_reports_only_its_own_interrupted_records(real_state):
    real_state.touched += [SESSION_A, SESSION_B]
    record_interrupted(SESSION_A, RECORD_A)
    record_interrupted(SESSION_B, RECORD_B)
    session_a = make_session(SESSION_A, FakeAgent(script=["block"]))

    async with running_turn(session_a) as run_a:
        session_a.cancel()
        outcome = await asyncio.wait_for(run_a, 10)

    assert outcome.stop_reason == "cancelled"
    assert outcome.field_meta == {
        "codePuppy/interrupted": [
            {"agentName": "retriever", "subSessionId": "sub-a", "savedCount": 3}
        ]
    }
    assert subagents.peek_interrupted_subagents(SESSION_A) == [RECORD_A]
    assert subagents.peek_interrupted_subagents(SESSION_B) == [RECORD_B]


async def test_normal_turn_has_no_interrupted_meta_even_with_queued_records(
    real_state,
):
    real_state.touched.append(SESSION_A)
    record_interrupted(SESSION_A, RECORD_A)
    session = make_session(SESSION_A)

    outcome = await session.prompt([text_block("hi")])

    assert outcome.stop_reason == "end_turn"
    assert outcome.field_meta is None
