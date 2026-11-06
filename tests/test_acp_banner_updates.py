"""ACP tool-call updates are built from the shared banner contract in ``code_puppy.banners``.

Titles, kinds and the ``_meta.code_puppy.banner`` payload must equal what the terminal
renders, so these tests compare the wire output against ``banner_for_call`` /
``banner_for_result`` rather than hard-coding labels.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio
from code_puppy.banners import banner_for_call, banner_for_result, result_text
from code_puppy.tools.command_runner import ShellCommandOutput
from code_puppy.tools.common import resolve_path
from code_puppy.tools.file_operations import ReadFileOutput

from code_puppy_core_plugins.acp import state
from code_puppy_core_plugins.acp.bridge import _MAX_DIFF_CHARS, EventBridge

QUESTIONS = {"questions": [{"question": "Which color?", "options": ["red", "blue"]}]}
DIFF_SHORT = "--- a\n+++ b\n@@\n-x\n+y\n"
DIFF_LONG = "+" * 9000

# (tool, args, arg holding the file location or None)
START_CASES = [
    pytest.param("read_file", {"file_path": "sample.txt"}, "file_path", id="read"),
    pytest.param("list_files", {"directory": "src"}, "directory", id="list"),
    pytest.param("agent_run_shell_command", {"command": "pytest -q"}, None, id="shell"),
    pytest.param("edit_file", {"file_path": "a.py"}, "file_path", id="edit"),
    pytest.param("create_file", {"file_path": "/tmp/new.py"}, "file_path", id="create"),
    pytest.param("replace_in_file", {"file_path": "b.py"}, "file_path", id="replace"),
    pytest.param("grep", {"search_string": "TODO"}, None, id="grep"),
    pytest.param("delete_file", {"file_path": "old.txt"}, "file_path", id="delete"),
]

# (tool, args, result, status, text prefix, banner error, banner exitCode)
FINISH_CASES = [
    pytest.param(
        "read_file",
        {"file_path": "sample.txt"},
        "file body",
        "completed",
        "file body",
        None,
        None,
        id="success-text",
    ),
    pytest.param(
        "read_file",
        {"file_path": "x"},
        ReadFileOutput(content="", num_tokens=0, error="File not found: x"),
        "failed",
        "File not found: x",
        "File not found: x",
        None,
        id="read-error-model",
    ),
    pytest.param(
        "agent_run_shell_command",
        {"command": "pytest -q"},
        ShellCommandOutput(
            success=False,
            command="pytest -q",
            stdout="",
            stderr="boom err",
            exit_code=3,
        ),
        "failed",
        "exit code 3: boom err",
        "exit code 3: boom err",
        3,
        id="shell-nonzero",
    ),
    pytest.param(
        "agent_run_shell_command",
        {"command": "pytest -q"},
        ShellCommandOutput(
            success=True, command="pytest -q", stdout="ok", stderr="", exit_code=0
        ),
        "completed",
        "ok",
        None,
        0,
        id="shell-zero",
    ),
    pytest.param(
        "agent_run_shell_command",
        {"command": "pytest -q"},
        ShellCommandOutput(
            success=False,
            command="pytest -q",
            stdout=None,
            stderr=None,
            exit_code=None,
            timeout=True,
        ),
        "failed",
        "timed out",
        "timed out",
        None,
        id="shell-timeout",
    ),
    pytest.param(
        "grep",
        {"search_string": "x"},
        Exception("boom"),
        "failed",
        "Exception: boom",
        "Exception: boom",
        None,
        id="exception",
    ),
    pytest.param(
        "edit_file",
        {"file_path": "a.py"},
        {"success": False, "error": "nope"},
        "failed",
        "nope",
        "nope",
        None,
        id="dict-failure",
    ),
]


class _Connection:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))


def _wire(update: Any) -> dict[str, Any]:
    return update.model_dump(by_alias=True, exclude_none=True)


def _banner_meta(update: Any) -> dict[str, Any]:
    return _wire(update)["_meta"]["code_puppy"]["banner"]


def _texts(update: Any) -> list[str]:
    return [item["content"]["text"] for item in _wire(update).get("content", [])]


@pytest_asyncio.fixture
async def connection():
    conn = _Connection()
    state.set_connection(conn, asyncio.get_running_loop())
    yield conn
    state.end_run()
    state.set_connection(None, None)


@pytest_asyncio.fixture
async def run(connection):
    state.begin_run("s1")
    yield connection


@pytest.mark.asyncio
@pytest.mark.parametrize("tool, args, path_arg", START_CASES)
async def test_start_update_matches_banner_contract(run, tool, args, path_arg):
    banner = banner_for_call(tool, args)

    await EventBridge()._on_pre_tool_call(tool, args)

    [(session_id, update)] = run.updates
    assert session_id == "s1"
    assert update.session_update == "tool_call"
    assert update.title == banner.title
    assert not update.title.startswith(("Read ", "Run:"))
    assert update.kind == banner.kind
    assert update.status == "in_progress"
    assert _wire(update)["rawInput"] == args
    assert _banner_meta(update) == banner.to_wire()
    expected_locations = [{"path": resolve_path(args[path_arg])}] if path_arg else None
    assert _wire(update).get("locations") == expected_locations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, args, result, status, text_prefix, error, exit_code", FINISH_CASES
)
async def test_finish_update_reports_result(
    run, tool, args, result, status, text_prefix, error, exit_code
):
    bridge = EventBridge()
    await bridge._on_pre_tool_call(tool, args)
    [(_, start)] = run.updates

    await bridge._on_post_tool_call(tool, args, result, 12.5)

    assert len(run.updates) == 2
    _, update = run.updates[-1]
    banner = _banner_meta(update)
    assert update.session_update == "tool_call_update"
    assert update.tool_call_id == start.tool_call_id
    assert update.status == status
    assert update.title == banner_for_result(tool, args, result, 12.5).title
    assert _wire(update)["rawOutput"] == EventBridge._summarize(result)
    assert banner["event"] == "tool_result"
    assert banner["durationMs"] == 12.5
    assert banner.get("error") == error
    assert banner.get("exitCode") == exit_code
    assert _texts(update)[0].startswith(text_prefix)
    assert _texts(update)[0] == result_text(result)
    if error is not None:
        assert _texts(update)[0].splitlines()[0] == banner["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "diff, expected_block",
    [
        pytest.param(DIFF_SHORT, f"```diff\n{DIFF_SHORT}\n```", id="short"),
        pytest.param(
            DIFF_LONG,
            "```diff\n" + "+" * (_MAX_DIFF_CHARS - 3) + "...\n```",
            id="truncated",
        ),
    ],
)
async def test_edit_diff_is_appended_as_fenced_block(run, diff, expected_block):
    bridge = EventBridge()
    args = {"file_path": "b.py"}
    await bridge._on_pre_tool_call("replace_in_file", args)

    await bridge._on_post_tool_call(
        "replace_in_file", args, {"success": True, "diff": diff}, 1.0
    )

    texts = _texts(run.updates[-1][1])
    assert len(texts) == 2
    assert texts[-1] == expected_block


@pytest.mark.asyncio
async def test_result_text_is_cut_to_4000_chars(run):
    bridge = EventBridge()
    await bridge._on_pre_tool_call("read_file", {"file_path": "big.txt"})

    await bridge._on_post_tool_call(
        "read_file", {"file_path": "big.txt"}, "x" * 5000, 1.0
    )

    text = _texts(run.updates[-1][1])[0]
    assert len(text) <= 4000
    assert text.endswith("…")


@pytest.mark.asyncio
async def test_no_active_session_sends_nothing(connection):
    bridge = EventBridge()
    args = {"file_path": "a.py"}

    assert await bridge._on_pre_tool_call("read_file", args) is None
    assert await bridge._on_post_tool_call("read_file", args, "x", 1.0) is None

    assert connection.updates == []


@pytest.mark.asyncio
async def test_ask_user_question_blocked_without_card_opt_in(run):
    result = await EventBridge()._on_pre_tool_call("ask_user_question", QUESTIONS)

    assert result["blocked"] is True
    assert result["error_message"].startswith("[BLOCKED]")
    assert run.updates == []
