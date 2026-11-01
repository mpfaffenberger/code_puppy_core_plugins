"""ACP composer wiring against the REAL new core APIs (no fakes of them).

The whole module is skipped on the old core, where these APIs do not exist.
Only the agent run is faked; the pause controllers, interrupted-subagent store,
command catalog and binary-media lookup are the real core objects.
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
from code_puppy_core_plugins.acp.agent import CodePuppyAgent  # noqa: E402
from code_puppy_core_plugins.acp.session import ACPSession  # noqa: E402
from tests.acp_session_support import FakeAgent, text_block  # noqa: E402

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
    monkeypatch.setattr(persistence, "delete", lambda session_id: None)
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


async def _until(predicate) -> None:
    async def spin() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(spin(), 10)


async def _start_blocked_turn(session: ACPSession) -> asyncio.Task:
    before = len(session.agent.calls)
    task = asyncio.ensure_future(session.prompt([text_block("go")]))
    await _until(lambda: len(session.agent.calls) > before)
    return task


async def _finish_turn(session: ACPSession, task: asyncio.Task) -> Any:
    session.agent.release()
    return await asyncio.wait_for(task, 10)


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
    # Cold catalog load (plugin discovery) takes ~50s; load it once per module.
    return composer_ext.available_commands()


def test_available_commands_expose_real_catalog_meta(catalog):
    commands = catalog
    by_name = {command.name: command for command in commands}

    assert len(commands) > 4  # the old ACP list exposed four commands
    assert all(not command.name.startswith("/") for command in commands)
    for command in commands:
        assert set(command.field_meta[COMMAND_KEY]) == {
            "interactive",
            "aliases",
            "category",
        }
    assert by_name["model"].field_meta[COMMAND_KEY]["interactive"] is True
    assert by_name["agent"].field_meta[COMMAND_KEY]["interactive"] is True
    assert by_name["help"].field_meta[COMMAND_KEY]["interactive"] is False


def test_commands_with_usage_hints_dump_hint_input(catalog):
    by_name = {command.name: command for command in catalog}

    wire = by_name["model"].model_dump(by_alias=True, exclude_none=True, mode="json")

    assert set(wire["input"]) == {"hint"}
    assert wire["input"]["hint"]


# ---- Steering with real per-session controllers ---------------------------
async def test_steer_queues_only_on_the_steered_session(real_state):
    real_state.touched += [SESSION_A, SESSION_B]
    session_a = ACPSession(SESSION_A, FakeAgent(script=["block"]))
    session_b = ACPSession(SESSION_B, FakeAgent(script=["block", "block"]))
    run_a = await _start_blocked_turn(session_a)
    run_b = await _start_blocked_turn(session_b)

    assert session_a.steer([text_block("nudge A")]) == {"accepted": True}

    assert _pending_now(SESSION_A) == ["nudge A"]
    assert _pending_now(SESSION_B) == []
    assert pause.get_pause_controller().pending_steer_counts() == (0, 0)

    await _finish_turn(session_b, run_b)
    run_b = await _start_blocked_turn(session_b)

    assert _pending_now(SESSION_A) == ["nudge A"]
    assert _pending_now(SESSION_B) == []

    assert session_b.steer([text_block("nudge B")]) == {"accepted": True}

    assert _pending_now(SESSION_B) == ["nudge B"]
    assert _pending_now(SESSION_A) == ["nudge A"]
    assert pause.get_pause_controller().pending_steer_counts() == (0, 0)
    # ACP steers are editor-owned: none may reach the terminal history file.
    assert real_state.saved_history == []

    await _finish_turn(session_a, run_a)
    await _finish_turn(session_b, run_b)


# ---- Interrupted-subagent records (real store) ----------------------------
async def test_cancelled_turn_reports_only_its_own_interrupted_records(real_state):
    real_state.touched += [SESSION_A, SESSION_B]
    record_interrupted(SESSION_A, RECORD_A)
    record_interrupted(SESSION_B, RECORD_B)
    session_a = ACPSession(SESSION_A, FakeAgent(script=["block"]))
    run_a = await _start_blocked_turn(session_a)

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
    session = ACPSession(SESSION_A, FakeAgent())

    outcome = await session.prompt([text_block("hi")])

    assert outcome.stop_reason == "end_turn"
    assert outcome.field_meta is None


# ---- close_session releases real state ------------------------------------
async def test_close_session_drops_real_controller_and_records(real_state, monkeypatch):
    monkeypatch.setattr(CodePuppyAgent, "_new_agent", staticmethod(lambda: FakeAgent()))
    agent = CodePuppyAgent()
    session_a = (await agent.new_session(cwd="/w")).session_id
    session_b = (await agent.new_session(cwd="/w")).session_id
    real_state.touched += [session_a, session_b]
    controller_a = pause.get_session_pause_controller(session_a)
    controller_b = pause.get_session_pause_controller(session_b)
    record_interrupted(session_a, RECORD_A)
    record_interrupted(session_b, RECORD_B)

    await agent.close_session(session_a)

    assert pause.get_session_pause_controller(session_a) is not controller_a
    assert subagents.peek_interrupted_subagents(session_a) == []
    assert pause.get_session_pause_controller(session_b) is controller_b
    assert subagents.peek_interrupted_subagents(session_b) == [RECORD_B]


# ---- Binary media and capabilities (real lookups) -------------------------
def test_binary_media_types_for_unknown_model_is_a_str_list():
    media_types = composer_ext.binary_media_types("<unknown-model-name>")

    assert isinstance(media_types, list)
    assert all(isinstance(media_type, str) for media_type in media_types)


def test_capabilities_meta_advertises_steer_and_binary_media():
    meta = composer_ext.capabilities_meta()

    assert meta["codePuppy/steer"] is True
    assert isinstance(meta["codePuppy/binaryMediaTypes"], list)
