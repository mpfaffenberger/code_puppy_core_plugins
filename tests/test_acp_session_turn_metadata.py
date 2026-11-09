"""ACPSession wires per-turn metadata out of the agent run into PromptResult.meta."""

from __future__ import annotations

import asyncio
import contextvars
from types import SimpleNamespace

import pytest

from code_puppy_core_plugins.acp import persistence, turn_metadata
from code_puppy_core_plugins.acp.session import ACPSession, PromptResult


@pytest.fixture(autouse=True)
def clean_registry():
    saved = list(turn_metadata._providers)
    turn_metadata._providers.clear()
    yield
    turn_metadata._providers[:] = saved


@pytest.fixture(autouse=True)
def no_persist(monkeypatch):
    monkeypatch.setattr(persistence, "save", lambda *args, **kwargs: None)


class _Agent:
    """Fake agent; ``model`` may be an exception instance to make the lookup fail."""

    def __init__(self, model="m1", run=None):
        self.model = model
        self.calls: list[str] = []
        self._run = run
        self._history: list = []

    def get_model_name(self):
        if isinstance(self.model, Exception):
            raise self.model
        return self.model

    async def run_with_mcp(self, text, attachments=None, link_attachments=None):
        self.calls.append(text)
        if self._run is None:
            return SimpleNamespace(output="ok")
        return await self._run()

    def set_message_history(self, messages):
        self._history = list(messages)

    def get_message_history(self):
        return list(self._history)


def _blocks(text: str):
    return [SimpleNamespace(type="text", text=text)]


def _provider(value):
    def provider(agent, session_id):
        return value

    return provider


def _register(fn):
    turn_metadata.register_turn_metadata_provider(fn)


async def test_success_reports_model_without_provider():
    session = ACPSession("s1", _Agent())

    result = await session.prompt(_blocks("hi"))

    assert result.stop_reason == "end_turn"
    assert result.meta == {"model": "m1"}


async def test_success_merges_provider_correlation_id():
    _register(_provider({"correlationId": "corr-9"}))
    session = ACPSession("s1", _Agent())

    result = await session.prompt(_blocks("hi"))

    assert result.meta == {"model": "m1", "correlationId": "corr-9"}


async def test_provider_reads_contextvar_set_inside_the_run():
    corr = contextvars.ContextVar("corr_ctx", default=None)

    async def run():
        corr.set("corr-ctx")
        return SimpleNamespace(output="ok")

    def provider(agent, session_id):
        value = corr.get()
        return {"correlationId": value} if value else None

    _register(provider)
    session = ACPSession("s1", _Agent(run=run))

    result = await session.prompt(_blocks("hi"))

    assert result.meta == {"model": "m1", "correlationId": "corr-ctx"}
    # The var was set in the prompt's task, so the caller's context never sees it.
    assert corr.get() is None


async def test_failure_keeps_model_and_correlation_id():
    corr = contextvars.ContextVar("corr_err", default=None)

    async def run():
        corr.set("corr-err")
        raise RuntimeError("boom")

    def provider(agent, session_id):
        value = corr.get()
        return {"correlationId": value} if value else None

    _register(provider)
    session = ACPSession("s1", _Agent(run=run))

    result = await session.prompt(_blocks("hi"))

    assert result.stop_reason == "refusal"
    assert result.meta == {"model": "m1", "correlationId": "corr-err"}


async def test_cancellation_keeps_meta_and_cancel_and_wait_returns_promptly():
    corr = contextvars.ContextVar("corr_cancel", default=None)
    started = asyncio.Event()

    async def run():
        corr.set("corr-cancel")
        started.set()
        await asyncio.Event().wait()

    def provider(agent, session_id):
        value = corr.get()
        return {"correlationId": value} if value else None

    _register(provider)
    session = ACPSession("s1", _Agent(run=run))
    prompt_task = asyncio.create_task(session.prompt(_blocks("hi")))
    await started.wait()

    await asyncio.wait_for(session.cancel_and_wait(timeout=2.0), timeout=3.0)
    result = await asyncio.wait_for(prompt_task, timeout=2.0)

    assert result.stop_reason == "cancelled"
    assert result.meta == {"model": "m1", "correlationId": "corr-cancel"}


async def test_no_model_and_no_provider_gives_none_meta():
    session = ACPSession("s1", _Agent(model=RuntimeError("no model")))

    result = await session.prompt(_blocks("hi"))

    assert result.stop_reason == "end_turn"
    assert result.meta is None


async def test_invalid_correlation_id_is_omitted():
    _register(_provider({"correlationId": "bad value!"}))
    session = ACPSession("s1", _Agent())

    result = await session.prompt(_blocks("hi"))

    assert result.meta == {"model": "m1"}


async def test_meta_does_not_leak_into_next_turn():
    current = {"corr": "corr-1"}

    def provider(agent, session_id):
        return {"correlationId": current["corr"]} if current["corr"] else None

    _register(provider)
    agent = _Agent()
    session = ACPSession("s1", agent)
    first = await session.prompt(_blocks("one"))

    current["corr"] = None
    agent.model = RuntimeError("no model")
    second = await session.prompt(_blocks("two"))

    assert first.meta == {"model": "m1", "correlationId": "corr-1"}
    assert second.stop_reason == "end_turn"
    assert second.meta is None


async def test_empty_prompt_returns_no_meta():
    _register(_provider({"correlationId": "corr-9"}))
    agent = _Agent()
    session = ACPSession("s1", agent)

    result = await session.prompt(_blocks("   "))

    assert result.stop_reason == "end_turn"
    assert result.meta is None
    assert agent.calls == []


def test_promptresult_positional_construction_defaults_meta_to_none():
    result = PromptResult("end_turn", None)

    assert result.meta is None
