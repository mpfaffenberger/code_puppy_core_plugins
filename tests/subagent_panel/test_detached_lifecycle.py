"""Detached (/fork + background) rows survive the main agent's turn boundary.

Foreground ``invoke_agent`` rows belong to the turn that made them and are
retired when it ends. A fork or background agent is its own asyncio task,
so its row must keep ticking at the idle prompt until it finishes.
"""

from __future__ import annotations

import pytest

from code_puppy_core_plugins.subagent_panel import register_callbacks as rc
from code_puppy_core_plugins.subagent_panel import state


class FakeConsole:
    def __init__(self):
        self.printed: list[str] = []

    def print(self, *args):
        self.printed.append(args[0].plain if args else "")


class FakeBar:
    def __init__(self):
        self.calls: list[list] = []

    def set_panel_lines(self, lines):
        self.calls.append(list(lines))


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    state.clear()
    rc._stop_ticker()
    rc._push_state["t"] = 0.0
    rc._push_state["count"] = -1
    monkeypatch.setattr(rc, "_PUSH_MIN_INTERVAL", 0.0)
    yield
    rc._stop_ticker()
    state.clear()


@pytest.fixture
def bar(monkeypatch):
    fake = FakeBar()
    monkeypatch.setattr("code_puppy.messaging.bottom_bar.get_bottom_bar", lambda: fake)
    return fake


def _ids(rows):
    return {e["session_id"] for e in rows}


# =========================================================================
# state: detached bookkeeping
# =========================================================================


@pytest.mark.parametrize("flag", ["is_fork", "background"])
def test_is_detached_for_fork_and_background(flag):
    state.register("sid", "worker", "gpt", **{flag: True})
    assert state.is_detached(state.snapshot()[0])


def test_is_detached_false_for_foreground():
    state.register("sid", "worker", "gpt")
    assert not state.is_detached(state.snapshot()[0])


def test_pop_settled_keeps_running_detached_root_and_its_children():
    state.register("bg", "worker", "gpt", background=True)
    state.register("bg-kid", "helper", "gpt", parent="bg")
    state.mark_done("bg-kid")  # a finished child stays grouped under its root
    state.register("fg", "foreground", "gpt")

    settled = state.pop_settled()

    assert _ids(settled) == {"fg"}
    assert _ids(state.snapshot()) == {"bg", "bg-kid"}


def test_pop_settled_returns_completed_detached_tree():
    state.register("bg", "worker", "gpt", background=True)
    state.mark_done("bg")

    assert _ids(state.pop_settled()) == {"bg"}
    assert state.snapshot() == []


def test_foreground_busy_ignores_live_detached_rows():
    state.register("bg", "worker", "gpt", background=True)
    assert not state.foreground_busy()

    state.register("fg", "foreground", "gpt")
    assert state.foreground_busy()

    state.mark_done("fg")
    assert not state.foreground_busy()


# =========================================================================
# panel: run end + flush
# =========================================================================


async def test_run_end_keeps_detached_rows_and_ticker(bar):
    state.register("bg", "worker", "gpt", background=True)
    state.register("fg", "foreground", "gpt")
    rc._start_ticker()

    await rc._on_agent_run_end(agent_name="main")

    assert _ids(state.snapshot()) == {"bg"}
    assert rc._ticker_task is not None and not rc._ticker_task.done()
    assert len(bar.calls[-1]) == 1  # the surviving row is still painted


async def test_run_end_with_only_foreground_rows_collapses(bar):
    state.register("fg", "foreground", "gpt")
    rc._start_ticker()

    await rc._on_agent_run_end(agent_name="main")

    assert state.snapshot() == []
    assert rc._ticker_task is None
    assert bar.calls[-1] == []


async def test_cancel_still_clears_detached_rows(bar):
    state.register("bg", "worker", "gpt", background=True)
    await rc._on_agent_run_cancel("group-1")
    assert state.snapshot() == []


def test_flush_prints_foreground_group_but_leaves_live_detached_row(bar):
    console = FakeConsole()
    state.register("fg", "foreground", "gpt")
    state.register("bg", "worker", "gpt", background=True)
    state.mark_done("fg")

    rc._maybe_flush_group(console)

    assert any("foreground" in line for line in console.printed)
    assert not any("worker" in line for line in console.printed)
    assert _ids(state.snapshot()) == {"bg"}


def test_flush_waits_while_foreground_busy(bar):
    console = FakeConsole()
    state.register("fg", "foreground", "gpt")
    state.register("bg", "worker", "gpt", background=True)
    state.mark_done("bg")  # finished detached row waits for the swarm

    rc._maybe_flush_group(console)

    assert console.printed == []
    assert _ids(state.snapshot()) == {"fg", "bg"}


def test_detached_row_finishing_at_idle_prompt_flushes_itself(bar):
    console = FakeConsole()
    state.register("bg", "worker", "gpt", background=True)
    rc._handle_frozen(console, "bg")

    assert any("worker" in line for line in console.printed)
    assert state.snapshot() == []


@pytest.mark.parametrize("context", [{"detached": True}, {"detached_fork": True}])
async def test_post_tool_call_retires_detached_row_at_boundary(bar, context):
    class Result:
        session_id = "bg"
        error = None

    state.register("bg", "worker", "gpt", background=True)
    await rc._on_post_tool_call("invoke_agent", {}, Result(), 1.0, context)
    assert state.snapshot() == []
