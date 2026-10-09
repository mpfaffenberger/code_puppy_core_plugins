"""ACP clients can opt in to presenting ``ask_user_question`` themselves.

By default the bridge blocks the tool and tells the model to ask in plain
text. A client that sets ``clientCapabilities._meta.codePuppyQuestionCards``
instead receives the questions as a completed tool call, and the model gets a
handled ``tool_result`` telling it to wait for the answers.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio
from acp.schema import ClientCapabilities

from code_puppy_core_plugins.acp import capabilities, io_delegation, permissions, state
from code_puppy_core_plugins.acp.agent import CodePuppyAgent
from code_puppy_core_plugins.acp.bridge import EventBridge

QUESTIONS = {"questions": [{"question": "Which color?", "options": ["red", "blue"]}]}


class _Connection:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))


def _caps(meta: Any) -> ClientCapabilities:
    return ClientCapabilities.model_validate({"_meta": meta})


@pytest.mark.parametrize(
    "caps, expected",
    [
        (None, False),
        (ClientCapabilities(), False),
        (_caps({"codePuppyQuestionCards": True}), True),
        (_caps({"codePuppyQuestionCards": "yes"}), False),
        (_caps({"somethingElse": True}), False),
    ],
)
def test_opt_in_requires_the_literal_flag(caps, expected):
    assert capabilities.client_presents_questions(caps) is expected


@pytest.mark.asyncio
async def test_initialize_enables_cards_only_for_opted_in_clients():
    agent = CodePuppyAgent()
    try:
        await agent.initialize(protocol_version=1, client_capabilities=None)
        assert agent._bridge.question_cards_enabled is False

        await agent.initialize(
            protocol_version=1,
            client_capabilities=_caps({"codePuppyQuestionCards": True}),
        )
        assert agent._bridge.question_cards_enabled is True
    finally:
        io_delegation.uninstall()
        permissions.uninstall()


@pytest_asyncio.fixture
async def run():
    conn = _Connection()
    state.set_connection(conn, asyncio.get_running_loop())
    state.begin_run("s1")
    yield conn
    state.end_run()
    state.set_connection(None, None)


@pytest.mark.asyncio
async def test_opted_in_client_receives_the_questions(run):
    bridge = EventBridge()
    bridge.question_cards_enabled = True

    result = await bridge._on_pre_tool_call("ask_user_question", dict(QUESTIONS))

    assert result["blocked"] is True
    assert "wait for the user's answers" in result["tool_result"]
    [(session_id, update)] = run.updates
    assert session_id == "s1"
    assert update.session_update == "tool_call"
    assert update.title == "ask_user_question"
    assert update.status == "completed"
    assert update.raw_input == QUESTIONS
    # Nothing left open for post_tool_call to close.
    assert state.current_tool_call() is None


@pytest.mark.asyncio
async def test_default_client_still_gets_the_plain_text_guidance(run):
    bridge = EventBridge()

    result = await bridge._on_pre_tool_call("ask_user_question", dict(QUESTIONS))

    assert result["blocked"] is True
    assert "tool_result" not in result
    assert "[BLOCKED]" in result["error_message"]
    assert run.updates == []


@pytest.mark.asyncio
async def test_other_tools_are_unaffected_by_the_opt_in(run):
    bridge = EventBridge()
    bridge.question_cards_enabled = True

    result = await bridge._on_pre_tool_call("read_file", {"file_path": "a.py"})

    assert result is None
    [(_, update)] = run.updates
    assert update.status == "in_progress"
    state.pop_tool_call("read_file")
