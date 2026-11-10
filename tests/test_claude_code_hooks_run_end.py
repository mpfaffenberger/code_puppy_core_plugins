"""The Claude hook bridge picks Stop vs SubagentStop from the run, not the name.

A helper named ``research-helper`` must raise ``SubagentStop`` while its parent
is still working, and only the root run's end raises ``Stop``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from code_puppy import callbacks
from code_puppy.callbacks import on_agent_run_end
from code_puppy.tools.subagent_context import subagent_context

from code_puppy_core_plugins.claude_code_hooks import register_callbacks as bridge


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []

    async def process_event(event_type: str, data: Any) -> None:
        seen.append((event_type, data.tool_name))

    monkeypatch.setattr(
        bridge, "_hook_engine", SimpleNamespace(process_event=process_event)
    )
    # Keep real dispatch so the host's positional argument order is exercised,
    # without the other plugins' agent_run_end callbacks.
    monkeypatch.setitem(
        callbacks._callbacks, "agent_run_end", [bridge.on_agent_run_end_hook]
    )
    return seen


async def _end(name: str) -> None:
    await on_agent_run_end(name, "model", session_id="s", response_text="done")


@pytest.mark.parametrize(
    "helper_name", ["research-helper", "code-puppy", "retriever", "my-agent"]
)
async def test_a_helper_ends_with_subagent_stop_then_the_root_ends_with_one_stop(
    events: list[tuple[str, str]], helper_name: str
) -> None:
    with subagent_context(helper_name):
        await _end(helper_name)

    assert events == [("SubagentStop", helper_name)]

    await _end("code-puppy")

    assert events == [("SubagentStop", helper_name), ("Stop", "code-puppy")]


@pytest.mark.parametrize("root_name", ["code-puppy", "pack_leader", "bloodhound"])
async def test_a_root_run_stops_with_stop_whatever_its_agent_is_called(
    events: list[tuple[str, str]], root_name: str
) -> None:
    await _end(root_name)

    assert events == [("Stop", root_name)]
