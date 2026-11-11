"""Tool-card correlation follows runtime identity, not argument object identity."""

import asyncio
from types import SimpleNamespace

import pytest

from code_puppy_core_plugins.acp import state
from code_puppy_core_plugins.acp.bridge import EventBridge


@pytest.fixture
async def cards():
    updates = []

    async def session_update(session_id, update):
        updates.append((session_id, update))

    state.set_connection(
        SimpleNamespace(session_update=session_update), asyncio.get_running_loop()
    )
    state.begin_run("correlation-session")
    yield EventBridge(), updates
    state.end_run()
    state.set_connection(None, None)


@pytest.mark.parametrize("same_name", [True, False])
@pytest.mark.parametrize("reverse", [True, False])
@pytest.mark.parametrize("outcome", ["ok", "error", "blocked", "cancelled"])
async def test_runtime_ids_match_copied_arguments(cards, same_name, reverse, outcome):
    bridge, updates = cards
    names = ["shell", "shell" if same_name else "read_file"]
    arguments = [{"command": "first"}, {"command": "second"}]
    for index in range(2):
        await bridge._on_pre_tool_call(
            names[index], arguments[index], {"tool_call_id": f"runtime-{index}"}
        )
    starts = [update for _, update in updates]
    for index in [1, 0] if reverse else [0, 1]:
        await bridge._on_post_tool_call(
            names[index],
            dict(arguments[index]),
            f"output-{index}",
            1.0,
            {"tool_call_id": f"runtime-{index}", "outcome": outcome},
        )
    terminals = {update.tool_call_id: update for _, update in updates[2:]}
    assert len(terminals) == 2
    for index, started in enumerate(starts):
        terminal = terminals[started.tool_call_id]
        assert terminal.status == ("completed" if outcome == "ok" else "failed")
        if outcome not in {"blocked", "cancelled"}:
            assert terminal.raw_output == f"output-{index}"
    assert state.current_tool_call() is None


@pytest.mark.parametrize("outcome", ["ok", "error", "cancelled"])
async def test_duplicate_terminal_cannot_close_an_open_sibling(cards, outcome):
    bridge, updates = cards
    for identifier in ("first", "second"):
        await bridge._on_pre_tool_call("shell", {}, {"tool_call_id": identifier})
    for _ in range(2):
        await bridge._on_post_tool_call(
            "shell",
            {},
            "first result",
            1.0,
            {"tool_call_id": "first", "outcome": outcome},
        )
    assert len(updates) == 3
    assert state.current_tool_call() == ("shell", updates[1][1].tool_call_id)
    await bridge._on_post_tool_call(
        "shell", {}, "second result", 1.0, {"tool_call_id": "second"}
    )
    assert len(updates) == 4 and state.current_tool_call() is None


async def test_shared_arguments_do_not_override_runtime_identity(cards):
    bridge, updates = cards
    shared = {"command": "identical input"}
    for identifier in ("first", "second"):
        await bridge._on_pre_tool_call("shell", shared, {"tool_call_id": identifier})
    for identifier in ("first", "second"):
        await bridge._on_post_tool_call(
            "shell",
            shared,
            identifier,
            1.0,
            {"tool_call_id": identifier, "outcome": "ok"},
        )
    starts = [update for _, update in updates[:2]]
    ends = {update.tool_call_id: update.raw_output for _, update in updates[2:]}
    assert [ends[start.tool_call_id] for start in starts] == ["first", "second"]


async def test_unknown_or_missing_runtime_id_never_closes_identified_sibling(cards):
    bridge, updates = cards
    arguments = {"command": "same object"}
    await bridge._on_pre_tool_call("shell", arguments, {"tool_call_id": "active"})
    for context in ({"tool_call_id": "unknown"}, {}, None):
        await bridge._on_post_tool_call("shell", arguments, "unrelated", 1.0, context)
    assert len(updates) == 1
    await bridge._on_post_tool_call(
        "shell", {}, "own result", 1.0, {"tool_call_id": "active"}
    )
    assert len(updates) == 2
    assert updates[0][1].tool_call_id == updates[1][1].tool_call_id


async def test_legacy_serial_calls_remain_supported(cards):
    bridge, updates = cards
    arguments = {"command": "legacy"}
    await bridge._on_pre_tool_call("shell", arguments)
    await bridge._on_post_tool_call("shell", arguments, "legacy result", 1.0)
    assert len(updates) == 2
    assert updates[0][1].tool_call_id == updates[1][1].tool_call_id
    assert state.current_tool_call() is None
    opened = state.push_tool_call("legacy-direct")
    assert state.pop_tool_call("legacy-direct") == opened


@pytest.mark.parametrize("outcome", ["blocked", "cancelled"])
async def test_nonexecuted_content_never_repeats_private_arguments(cards, outcome):
    bridge, updates = cards
    private_args = {
        "command": "private denied argument",
        "file_path": "private denied argument",
    }
    await bridge._on_pre_tool_call(
        "shell", private_args, {"tool_call_id": "private-call"}
    )
    await bridge._on_post_tool_call(
        "shell",
        private_args,
        {"diff": "private denied argument", "error": "private denied argument"},
        1.0,
        {"tool_call_id": "private-call", "outcome": outcome},
    )
    terminal = updates[-1][1]
    assert "private denied argument" not in repr(
        [item.model_dump() for _, item in updates]
    )
    assert terminal.status == "failed"


async def test_captured_missing_identity_ignores_later_metadata_forgery(cards):
    try:
        from code_puppy.tool_identity import tool_call_identity
    except ImportError:
        pytest.skip("core does not support isolated hook identity")
    bridge, updates = cards
    arguments = {"command": "legacy"}
    with tool_call_identity({}):
        await bridge._on_pre_tool_call("shell", arguments, {"tool_call_id": "forged"})
        await bridge._on_post_tool_call("shell", arguments, "own result", 0, {})
    assert len(updates) == 2 and state.current_tool_call() is None


async def test_parallel_runs_do_not_share_card_identity(cards):
    bridge, updates = cards

    async def run(session):
        state.begin_run(session)
        await bridge._on_pre_tool_call("shell", {}, {"tool_call_id": "same-runtime"})
        await asyncio.sleep(0)
        await bridge._on_post_tool_call(
            "shell", {}, session, 1.0, {"tool_call_id": "same-runtime"}
        )
        assert state.current_tool_call() is None
        state.end_run()

    await asyncio.gather(run("session-a"), run("session-b"))
    for session in ("session-a", "session-b"):
        own = [update for identifier, update in updates if identifier == session]
        assert len(own) == 2
        assert own[0].tool_call_id == own[1].tool_call_id
        assert own[1].raw_output == session
