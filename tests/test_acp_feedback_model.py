"""Feedback records name the model that answered the last finished turn, not the current one."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from code_puppy_core_plugins.acp import (
    feedback,
    feedback_queue,
    feedback_summarizer,
    io_delegation,
    permissions,
    persistence,
    state,
    turn_metadata,
)
from code_puppy_core_plugins.acp.agent import CodePuppyAgent
from code_puppy_core_plugins.acp.feedback_summarizer import FeedbackSummary
from code_puppy_core_plugins.acp.session import ACPSession
from tests.acp_feedback_support import clean_feedback_state  # noqa: F401
from tests.acp_feedback_support import feedback_data_dir  # noqa: F401

pytestmark = pytest.mark.usefixtures("feedback_data_dir", "clean_feedback_state")

SESSION_ID = "sess_model"
SENTINEL = "SENTINEL-never-log"
AGENT_LOGGER = "code_puppy_core_plugins.acp.agent"


class _Agent:
    """Fake agent; an Exception ``model`` makes ``get_model_name`` raise."""

    def __init__(self, model: Any = "model-a") -> None:
        self.model = model
        self._history: list[Any] = []

    def get_model_name(self) -> Any:
        if isinstance(self.model, Exception):
            raise self.model
        return self.model

    async def run_with_mcp(
        self, text: str, attachments: Any = None, link_attachments: Any = None
    ) -> SimpleNamespace:
        return SimpleNamespace(output="ok")

    def set_message_history(self, messages: list[Any]) -> None:
        self._history = list(messages)

    def get_message_history(self) -> list[Any]:
        return list(self._history)


@pytest.fixture(autouse=True)
def triage(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Guard: no test reaches a real model; the mock is also the assertion target."""
    mock = AsyncMock(
        return_value=FeedbackSummary(tags=["incorrect"], reason="Generic cause")
    )
    monkeypatch.setattr(feedback_summarizer, "summarize", mock)
    return mock


@pytest.fixture(autouse=True)
def no_background_pass(monkeypatch: pytest.MonkeyPatch) -> Mock:
    schedule = Mock()
    monkeypatch.setattr(feedback, "schedule_processing", schedule)
    return schedule


@pytest.fixture(autouse=True)
def no_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(persistence, "save", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def clean_turn_providers() -> Any:
    saved = list(turn_metadata._providers)
    turn_metadata._providers.clear()
    yield
    turn_metadata._providers[:] = saved


@pytest.fixture
def agent() -> Any:
    instance = CodePuppyAgent()
    yield instance
    instance.shutdown()
    io_delegation.uninstall()
    permissions.uninstall()
    state.set_connection(None, None)


def _blocks(text: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(type="text", text=text)]


async def _run_turn(session: ACPSession, text: str = "hi") -> None:
    await session.prompt(_blocks(text))


def _inject(agent: CodePuppyAgent, fake: _Agent) -> ACPSession:
    session = ACPSession(SESSION_ID, fake)
    agent._sessions[SESSION_ID] = session
    return session


async def test_turn_model_survives_switch_and_feedback_stores_it(
    agent, no_background_pass
):
    fake = _Agent("model-a")
    _inject(agent, fake)
    await _run_turn(agent._sessions[SESSION_ID])
    fake.model = "model-b"

    response = await agent.ext_method(
        "code_puppy/feedback",
        {"sessionId": SESSION_ID, "rating": "down", "reasons": ["wrong"]},
    )

    assert response["accepted"] is True
    [record] = feedback_queue.read_all()
    assert record["id"] == response["feedbackId"]
    assert record["model"] == "model-a"
    no_background_pass.assert_called_once_with()


@pytest.mark.parametrize(
    "session_id, model, expected",
    [
        pytest.param(SESSION_ID, "model-x", "model-x", id="no-turn-yet-current-model"),
        pytest.param("sess_missing", "model-x", None, id="unknown-session"),
        pytest.param(SESSION_ID, RuntimeError("boom"), None, id="raising-agent"),
    ],
)
def test_model_before_any_finished_turn(agent, session_id, model, expected):
    _inject(agent, _Agent(model))

    assert agent._model_for_session(session_id) == expected


async def test_last_turn_model_survives_agent_rebind(agent):
    _inject(agent, _Agent("model-a"))
    session = agent._sessions[SESSION_ID]
    await _run_turn(session)

    session.agent = _Agent("model-c")

    assert agent._model_for_session(SESSION_ID) == "model-a"


async def test_later_turn_with_different_model_updates_it(agent):
    fake = _Agent("model-a")
    _inject(agent, fake)
    session = agent._sessions[SESSION_ID]
    await _run_turn(session)

    fake.model = "model-b"
    await _run_turn(session)

    assert agent._model_for_session(SESSION_ID) == "model-b"


async def test_early_return_prompt_keeps_last_turn_model(agent):
    _inject(agent, _Agent("model-a"))
    session = agent._sessions[SESSION_ID]
    await _run_turn(session)

    await _run_turn(session, text="   ")

    assert agent._model_for_session(SESSION_ID) == "model-a"


async def test_turn_with_failed_model_lookup_keeps_previous_model(agent):
    fake = _Agent("model-a")
    _inject(agent, fake)
    session = agent._sessions[SESSION_ID]
    await _run_turn(session)

    fake.model = RuntimeError("no model")
    await _run_turn(session)

    assert agent._model_for_session(SESSION_ID) == "model-a"


def test_model_lookup_failure_logs_exception_type_only(agent, caplog):
    _inject(agent, _Agent(RuntimeError(SENTINEL)))

    with caplog.at_level(logging.DEBUG, logger=AGENT_LOGGER):
        assert agent._model_for_session(SESSION_ID) is None

    [record] = [r for r in caplog.records if r.name == AGENT_LOGGER]
    assert not record.exc_info
    assert "RuntimeError" in record.getMessage()
    assert all(SENTINEL not in r.getMessage() for r in caplog.records)
