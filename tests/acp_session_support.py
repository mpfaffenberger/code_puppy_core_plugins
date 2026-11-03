"""Shared fakes for the ACP session turn and steer tests.

``isolate_session_core`` (autouse once a test module imports it) makes the old
core the default: new-core APIs are removed or blocked, persistence is stubbed,
and no connection is registered, so ``ACPSession.prompt`` runs fully in memory.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterable, Optional

import pytest
from acp.schema import TextContentBlock

from code_puppy_core_plugins.acp import persistence, state
from code_puppy_core_plugins.acp.session import ACPSession

PAUSE_MODULE = "code_puppy.messaging.pause_controller"
SUBAGENT_MODULE = "code_puppy.tools.subagent_invocation"
MEDIA_MODULE = "code_puppy.model_media"

_NEW_CORE_ATTRS = {
    PAUSE_MODULE: ("get_session_pause_controller", "drop_session_pause_controller"),
    SUBAGENT_MODULE: ("peek_interrupted_subagents", "clear_interrupted_subagents"),
}


class FakeResult:
    """Stand-in for a pydantic-ai run result."""

    def __init__(self, output: str = "done", messages: Optional[Iterable[Any]] = None):
        self.output = output
        self.usage = None
        self._messages = list(messages or [f"msg:{output}"])

    def all_messages(self) -> list[Any]:
        return list(self._messages)


class FakeAgent:
    """Scripted ``BaseAgent``: each ``run_with_mcp`` call pops the next behaviour.

    ``finish`` returns at once. ``block`` and ``permission`` await the current
    gate (``permission`` also flags that the run waits on a client reply).
    ``fail`` awaits the gate, then raises.
    """

    def __init__(
        self,
        model_name: Any = "m-text",
        script: Iterable[str] = (),
        history: Iterable[Any] = (),
    ) -> None:
        self.model_name = model_name
        self._script = list(script)
        self._history = list(history)
        self.gate = asyncio.Event()
        self.waiting_on: Optional[str] = None
        self.calls: list[dict[str, Any]] = []

    @property
    def awaiting_permission(self) -> bool:
        return self.waiting_on == "permission"

    def release(self) -> None:
        self.gate.set()

    async def run_with_mcp(
        self, text: str, attachments: Any = None, link_attachments: Any = None
    ) -> FakeResult:
        self.calls.append(
            {
                "text": text,
                "attachments": attachments,
                "link_attachments": link_attachments,
            }
        )
        behaviour = self._script.pop(0) if self._script else "finish"
        if behaviour == "finish":
            return FakeResult()
        self.gate = asyncio.Event()
        self.waiting_on = behaviour
        await self.gate.wait()
        if behaviour == "fail":
            raise RuntimeError("boom")
        return FakeResult()

    def get_model_name(self) -> Any:
        return self.model_name

    def get_message_history(self) -> list[Any]:
        return list(self._history)

    def set_message_history(self, messages: list[Any]) -> None:
        self._history = list(messages)


class FakeController:
    def __init__(self) -> None:
        self.steers: list[tuple[str, str, bool]] = []

    def request_steer(
        self, text: str, mode: str = "now", *, history_recorded: bool = False
    ) -> None:
        self.steers.append((text, mode, history_recorded))


class FakePauseStore:
    """Per-session pause controllers, mirroring ``get_session_pause_controller``."""

    def __init__(self) -> None:
        self._controllers: dict[str, FakeController] = {}

    def __call__(self, session_id: str) -> FakeController:
        return self.controller_for(session_id)

    def controller_for(self, session_id: str) -> FakeController:
        return self._controllers.setdefault(session_id, FakeController())


class FakeSubagentStore:
    """Keyed interrupted-subagent records; records drain/clear calls as spies."""

    def __init__(self) -> None:
        self.records: dict[str, list[dict[str, Any]]] = {}
        self.drain_calls: list[Any] = []
        self.clear_calls: list[Any] = []

    def peek(self, session_id: str) -> list[dict[str, Any]]:
        return [dict(record) for record in self.records.get(session_id, [])]

    def drain(self, *args: Any) -> list[dict[str, Any]]:
        self.drain_calls.append(args)
        return []

    def clear(self, *args: Any) -> None:
        self.clear_calls.append(args)


def install_pause_store(monkeypatch: pytest.MonkeyPatch) -> FakePauseStore:
    store = FakePauseStore()
    module = importlib.import_module(PAUSE_MODULE)
    monkeypatch.setattr(module, "get_session_pause_controller", store, raising=False)
    return store


def _subagent_module() -> types.ModuleType:
    # Old core: importing subagent_invocation first hits a circular import via agent_tools.
    importlib.import_module("code_puppy.tools.agent_tools")
    return importlib.import_module(SUBAGENT_MODULE)


def install_subagent_store(monkeypatch: pytest.MonkeyPatch) -> FakeSubagentStore:
    store = FakeSubagentStore()
    module = _subagent_module()
    monkeypatch.setattr(module, "peek_interrupted_subagents", store.peek, raising=False)
    monkeypatch.setattr(
        module, "drain_interrupted_subagents", store.drain, raising=False
    )
    monkeypatch.setattr(
        module, "clear_interrupted_subagents", store.clear, raising=False
    )
    return store


def install_binary_media(
    monkeypatch: pytest.MonkeyPatch, supported: dict[str, list[str]]
) -> None:
    module = types.ModuleType(MEDIA_MODULE)
    module.supported_binary_media_types = lambda model: list(supported.get(model, []))
    monkeypatch.setitem(sys.modules, MEDIA_MODULE, module)


def text_block(text: str) -> TextContentBlock:
    return TextContentBlock(type="text", text=text)


def make_session(
    session_id: str = "s1", agent: Optional[FakeAgent] = None
) -> ACPSession:
    return ACPSession(session_id, agent or FakeAgent())


@asynccontextmanager
async def running_turn(
    session: ACPSession, blocks: Optional[list[Any]] = None
) -> AsyncIterator[asyncio.Task[Any]]:
    """Start a prompt and yield once its agent run is in flight.

    On exit the run's gate is released and the prompt task is awaited, so the
    yielded task always holds the turn's result.
    """
    agent = session.agent
    before = len(agent.calls)
    task = asyncio.ensure_future(session.prompt(blocks or [text_block("go")]))
    while len(agent.calls) == before:
        await asyncio.sleep(0)
    try:
        yield task
    finally:
        agent.release()
        await task


@pytest.fixture(autouse=True)
def isolate_session_core(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(persistence, "save", lambda *args, **kwargs: None)
    monkeypatch.setattr(state, "_CONNECTION", None)
    monkeypatch.setattr(state, "_LOOP", None)
    pause = importlib.import_module(PAUSE_MODULE)
    for attr in _NEW_CORE_ATTRS[PAUSE_MODULE]:
        monkeypatch.delattr(pause, attr, raising=False)
    subagents = _subagent_module()
    for attr in _NEW_CORE_ATTRS[SUBAGENT_MODULE]:
        monkeypatch.delattr(subagents, attr, raising=False)
    monkeypatch.setitem(sys.modules, MEDIA_MODULE, None)
