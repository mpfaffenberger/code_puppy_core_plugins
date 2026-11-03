"""ACP session folders: each session anchors its agent rules and tools to its own cwd.

The fake agent uses the real AGENTS.md loader and the real shell tool, so the
assertions observe the folder a command runs in, not a stored attribute. Nothing
here calls ``os.chdir``; a test that did would break the other concurrent sessions.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from acp.schema import TextContentBlock
from pydantic_ai.messages import ModelRequest, UserPromptPart

from code_puppy.agents import _builder
from code_puppy.tools import command_runner
from code_puppy.tools.common import (
    get_working_directory,
    reset_working_directory,
    set_working_directory,
)
from code_puppy_core_plugins.acp import folder, persistence, replay, state
from code_puppy_core_plugins.acp.agent import CodePuppyAgent

PRINT_CWD = f'"{sys.executable}" -c "import os;print(os.getcwd())"'
MARKER_A = "MARKER-ALPHA-7731"
MARKER_B = "MARKER-BRAVO-4419"
MARKER_C = "MARKER-CHARLIE-2086"
STOP_END_TURN = "end_turn"


def _core_honors_working_directory() -> bool:
    # Core must read the per-chat folder (core PR mpfaffenberger/code_puppy#1097); older core ignores it.
    marker = "PROBE-CORE-FOLDER-5513"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            with open(
                os.path.join(tmp, "AGENTS.md"), "w", encoding="utf-8"
            ) as rules_file:
                rules_file.write(f"{marker}\n")
            token = set_working_directory(tmp)
            try:
                rules = _builder.load_puppy_rules() or ""
                with patch("code_puppy.config.get_yolo_mode", return_value=True):
                    result = asyncio.run(
                        command_runner.run_shell_command(
                            MagicMock(), PRINT_CWD, None, 30, False
                        )
                    )
            finally:
                reset_working_directory(token)
            printed = os.path.realpath(result.stdout.strip())
            return marker in rules and printed == os.path.realpath(tmp)
    except Exception:
        return False


_needs_core_folder = pytest.mark.skipif(
    not _core_honors_working_directory(),
    reason="needs core PR mpfaffenberger/code_puppy#1097 (shell tool and AGENTS.md loading follow the chat folder); these tests run once core includes it",
)


class _RunResult:
    output = "ok"

    def all_messages(self):
        return []


class FakeAgent:
    """Stands in for BaseAgent: real rules loading and real shell tool."""

    def __init__(self):
        self.builds = 0
        self.runs = 0
        self.last_pwd = None
        self._history = []
        self.reload_code_generation_agent()

    def reload_code_generation_agent(self, *args, **kwargs):
        self.builds += 1
        self.instructions = _builder.load_puppy_rules() or ""

    async def run_with_mcp(self, text, **kwargs):
        self.runs += 1
        result = await command_runner.run_shell_command(
            MagicMock(), PRINT_CWD, None, 30, False
        )
        self.last_pwd = result.stdout.strip()
        return _RunResult()

    def get_message_history(self):
        return list(self._history)

    def set_message_history(self, history):
        self._history = list(history)


class FakeConnection:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append((session_id, update))

    def texts(self, session_id):
        return [
            update.content.text
            for sid, update in self.updates
            if sid == session_id and update.session_update == "agent_message_chunk"
        ]


@dataclass
class Harness:
    conn: FakeConnection
    saves: list


@pytest.fixture(autouse=True)
def _yolo_mode(monkeypatch):
    monkeypatch.setattr("code_puppy.config.get_yolo_mode", lambda: True)


@pytest_asyncio.fixture
async def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(CodePuppyAgent, "_new_agent", staticmethod(lambda: FakeAgent()))
    monkeypatch.setattr(persistence, "_base_dir", lambda: tmp_path / "acp-store")
    saves = []

    def _spy_save(session_id, agent, cwd=None, additional_directories=None, **_):
        saves.append((session_id, cwd))

    monkeypatch.setattr(persistence, "save", _spy_save)
    conn = FakeConnection()
    state.set_connection(conn, asyncio.get_running_loop())
    yield Harness(conn, saves)
    state.set_connection(None, None)


def _make_dir(root, name, marker=None):
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    if marker:
        (path / "AGENTS.md").write_text(f"{marker}\n", encoding="utf-8")
    return path


def _same(printed, expected):
    return printed is not None and os.path.realpath(printed) == os.path.realpath(
        str(expected)
    )


def _session(agent, session_id):
    # The private registry is the only handle to a session's own agent.
    return agent._sessions[session_id]


def _say(agent, session_id, text):
    return agent.prompt([TextContentBlock(type="text", text=text)], session_id)


async def _new(agent, folder_path):
    return (await agent.new_session(cwd=str(folder_path))).session_id


def _assert_rules(agent, session_id, own, other):
    rules = _session(agent, session_id).agent.instructions
    assert own in rules
    assert other not in rules


async def _noop_replay(session_id, history):
    return None


def _forbid_chdir(path):
    raise AssertionError(f"os.chdir must never run; called with {path!r}")


@_needs_core_folder
async def test_concurrent_sessions_keep_their_own_folder(tmp_path, harness):
    a = _make_dir(tmp_path, "A", MARKER_A)
    b = _make_dir(tmp_path, "B", MARKER_B)
    agent = CodePuppyAgent()
    sid_a = await _new(agent, a)
    sid_b = await _new(agent, b)
    _assert_rules(agent, sid_a, MARKER_A, MARKER_B)
    _assert_rules(agent, sid_b, MARKER_B, MARKER_A)

    await asyncio.gather(_say(agent, sid_a, "where am I"), _say(agent, sid_b, "where"))

    assert _same(_session(agent, sid_a).agent.last_pwd, a)
    assert _same(_session(agent, sid_b).agent.last_pwd, b)
    _assert_rules(agent, sid_a, MARKER_A, MARKER_B)
    _assert_rules(agent, sid_b, MARKER_B, MARKER_A)


@_needs_core_folder
@pytest.mark.parametrize("creation", ["load", "fork"])
async def test_restored_or_forked_session_uses_its_folder(
    tmp_path, harness, monkeypatch, creation
):
    a = _make_dir(tmp_path, "A", MARKER_A)
    b = _make_dir(tmp_path, "B", MARKER_B)
    monkeypatch.setattr(replay, "replay_history", _noop_replay)
    agent = CodePuppyAgent()
    if creation == "load":
        sid = "sess_restored"
        await agent.load_session(cwd=str(b), session_id=sid)
    else:
        source = await _new(agent, a)
        sid = (await agent.fork_session(cwd=str(b), session_id=source)).session_id

    _assert_rules(agent, sid, MARKER_B, MARKER_A)
    await _say(agent, sid, "where am I")
    assert _same(_session(agent, sid).agent.last_pwd, b)


@_needs_core_folder
async def test_cd_moves_only_the_current_session(tmp_path, harness, monkeypatch):
    a = _make_dir(tmp_path, "A", MARKER_A)
    b = _make_dir(tmp_path, "B", MARKER_B)
    c = _make_dir(tmp_path, "C", MARKER_C)
    process_cwd = os.getcwd()
    monkeypatch.setattr(os, "chdir", _forbid_chdir)
    agent = CodePuppyAgent()
    sid_a = await _new(agent, a)
    sid_b = await _new(agent, b)
    agent_b_before = _session(agent, sid_b).agent

    stop = await _say(agent, sid_a, f"/cd {c}")
    assert stop.stop_reason == STOP_END_TURN
    await _say(agent, sid_a, "where am I")

    assert _same(_session(agent, sid_a).agent.last_pwd, c)
    _assert_rules(agent, sid_a, MARKER_C, MARKER_A)
    session_b = _session(agent, sid_b)
    assert session_b.agent is agent_b_before
    assert _same(session_b.cwd, b)
    _assert_rules(agent, sid_b, MARKER_B, MARKER_C)
    await _say(agent, sid_b, "where am I")
    assert _same(session_b.agent.last_pwd, b)

    listed = {s.session_id: s.cwd for s in (await agent.list_sessions()).sessions}
    assert _same(listed[sid_a], c)
    assert _same(listed[sid_b], b)
    assert any(str(c) in text for text in harness.conn.texts(sid_a))
    assert any(sid == sid_a and _same(cwd, c) for sid, cwd in harness.saves)
    assert os.getcwd() == process_cwd


@_needs_core_folder
@pytest.mark.parametrize(
    "arg, expected",
    [("sub", "sub"), ('"with space"', "with space")],
)
async def test_cd_argument_resolves_against_session_folder(
    tmp_path, harness, arg, expected
):
    a = _make_dir(tmp_path, "A", MARKER_A)
    target = _make_dir(a, expected)
    agent = CodePuppyAgent()
    sid = await _new(agent, a)

    stop = await _say(agent, sid, f"/cd {arg}")
    await _say(agent, sid, "where am I")

    assert stop.stop_reason == STOP_END_TURN
    assert _same(_session(agent, sid).agent.last_pwd, target)


@pytest.mark.parametrize("arg", ["nope", "afile.txt"])
async def test_invalid_cd_target_changes_nothing(tmp_path, harness, arg):
    a = _make_dir(tmp_path, "A", MARKER_A)
    (a / "afile.txt").write_text("not a folder", encoding="utf-8")
    agent = CodePuppyAgent()
    sid = await _new(agent, a)
    session = _session(agent, sid)
    agent_before = session.agent
    builds_before = agent_before.builds
    texts_before = len(harness.conn.texts(sid))
    listed_before = (await agent.list_sessions()).sessions

    stop = await _say(agent, sid, f"/cd {arg}")

    assert stop.stop_reason == STOP_END_TURN
    assert len(harness.conn.texts(sid)) == texts_before + 1
    assert session.agent is agent_before
    assert agent_before.builds == builds_before
    assert agent_before.runs == 0
    assert _same(session.cwd, a)
    assert (await agent.list_sessions()).sessions == listed_before


async def test_bare_cd_reports_session_folder_and_changes_nothing(tmp_path, harness):
    a = _make_dir(tmp_path, "A", MARKER_A)
    agent = CodePuppyAgent()
    sid = await _new(agent, a)
    session = _session(agent, sid)
    agent_before = session.agent
    builds_before = agent_before.builds

    stop = await _say(agent, sid, "/cd")

    assert stop.stop_reason == STOP_END_TURN
    assert any(str(a) in text for text in harness.conn.texts(sid))
    assert session.agent is agent_before
    assert agent_before.builds == builds_before
    assert agent_before.runs == 0
    assert _same(session.cwd, a)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("/cd", ""),
        ("/cd  x", "x"),
        ('/cd "a b"', "a b"),
        ("/cdx", None),
        ("/help", None),
        ("hello", None),
    ],
)
def test_cd_argument_parses_only_cd_commands(text, expected):
    assert folder.cd_argument(text) == expected


def test_working_directory_sets_and_restores_override(tmp_path):
    target = _make_dir(tmp_path, "A")
    before = get_working_directory()
    with folder.working_directory(str(target)):
        assert get_working_directory() == str(target)
    assert get_working_directory() == before


@pytest.mark.parametrize("cwd", [None, ""])
def test_working_directory_falsy_cwd_is_noop(tmp_path, cwd):
    outer = _make_dir(tmp_path, "outer")
    token = set_working_directory(str(outer))
    try:
        with folder.working_directory(cwd):
            assert get_working_directory() == str(outer)
    finally:
        reset_working_directory(token)


_REAL_SAVE = persistence.save  # captured at import, before the harness installs its spy


async def test_cd_persists_new_folder_for_list_and_resume(
    tmp_path, harness, monkeypatch
):
    monkeypatch.setattr(persistence, "save", _REAL_SAVE)
    monkeypatch.setattr(replay, "replay_history", _noop_replay)
    x = _make_dir(tmp_path, "X")
    y = _make_dir(tmp_path, "Y")
    agent = CodePuppyAgent()
    sid = await _new(agent, x)
    _session(agent, sid).agent.set_message_history(
        [ModelRequest(parts=[UserPromptPart(content="hi")])]
    )

    await _say(agent, sid, f"/cd {y}")

    on_disk = {r.session_id: r.cwd for r in persistence.list_persisted()}
    assert _same(on_disk[sid], y)
    restarted = CodePuppyAgent()
    listed = {s.session_id: s.cwd for s in (await restarted.list_sessions()).sessions}
    assert _same(listed[sid], y)
    await restarted.load_session(cwd=listed[sid], session_id=sid)
    assert _same(_session(restarted, sid).cwd, y)
