"""ACP ``/session rename``: title persistence, client announcements, and listed titles."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from acp.schema import SessionInfoUpdate
from code_puppy.session_storage import save_session

from code_puppy_core_plugins.acp import commands, persistence, state
from code_puppy_core_plugins.acp.agent import CodePuppyAgent
from code_puppy_core_plugins.acp.session import ACPSession

try:
    from code_puppy.command_line.session_commands import (  # noqa: F401
        rename_from_command,
    )
    from code_puppy.session_storage import has_user_title  # noqa: F401
except ImportError:
    _HAS_RENAME_CORE = False
else:
    _HAS_RENAME_CORE = True

needs_rename_core = pytest.mark.skipif(
    not _HAS_RENAME_CORE, reason="installed core lacks the /session rename API"
)

SID = "acp-session-1"
LIVE = "acp-live"
SAVED = "acp-saved"
UNTITLED = "acp-untitled"
HANDLER = "code_puppy.command_line.command_handler.handle_command"
SESSION_COMMANDS = "code_puppy.command_line.session_commands"


class _Connection:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    autosave = tmp_path / "autosave"
    (autosave / "acp").mkdir(parents=True)
    monkeypatch.setattr(persistence, "_base_dir", lambda: autosave / "acp")
    monkeypatch.setattr("code_puppy.config.AUTOSAVE_DIR", str(autosave))
    monkeypatch.setattr(
        f"{SESSION_COMMANDS}.AUTOSAVE_DIR", str(autosave), raising=False
    )
    return autosave


@pytest_asyncio.fixture
async def connection():
    conn = _Connection()
    state.set_connection(conn, asyncio.get_running_loop())
    yield conn
    state.set_connection(None, None)


def _seed(
    root: Path,
    session_id: str = SID,
    *,
    cwd: str = "/work/project",
    additional: list[str] | None = None,
    timestamp: str = "2026-08-15T12:00:00",
) -> None:
    base = root / "acp"
    save_session(
        history=[],
        session_name=persistence._safe_name(session_id),
        base_dir=base,
        timestamp=timestamp,
        token_estimator=lambda _m: 1,
    )
    persistence._write_acp_meta(
        base,
        persistence.PersistedSession(
            session_id=session_id,
            cwd=cwd,
            additional_directories=additional,
            updated_at=timestamp,
        ),
    )


def _core_meta_path(root: Path, session_id: str = SID) -> Path:
    return root / "acp" / f"{persistence._safe_name(session_id)}_meta.json"


def _write_core_meta(root: Path, session_id: str, fields: dict[str, Any]) -> None:
    path = _core_meta_path(root, session_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data), encoding="utf-8")


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def _is_session_info(update: Any) -> bool:
    return isinstance(update, SessionInfoUpdate)


def test_read_title_is_none_without_session(root):
    assert persistence.read_title(SID) is None


@pytest.mark.parametrize(
    "fields", [{}, {"title": ""}, {"title": 42}, {"title": ["Mine"]}]
)
def test_read_title_ignores_missing_or_non_string_titles(root, fields):
    _seed(root)
    _write_core_meta(root, SID, fields)

    assert persistence.read_title(SID) is None


def test_read_title_is_none_for_corrupt_sidecar(root):
    _seed(root)
    _core_meta_path(root).write_text("{not json", encoding="utf-8")

    assert persistence.read_title(SID) is None


def test_read_title_returns_title(root):
    _seed(root)
    _write_core_meta(root, SID, {"title": "Mine"})

    assert persistence.read_title(SID) == "Mine"


def test_read_title_resolves_unsafe_session_id_to_safe_stem(root):
    _seed(root, "a/b")
    _write_core_meta(root, "a/b", {"title": "Mine"})

    assert (root / "acp" / "a_b_meta.json").exists()
    assert persistence.read_title("a/b") == "Mine"


@needs_rename_core
@pytest.mark.parametrize(
    "source, expected",
    [("user", "Mine"), ("auto", None), (None, None)],
)
def test_read_title_user_only_requires_user_title_source(root, source, expected):
    _seed(root)
    fields: dict[str, Any] = {"title": "Mine"}
    if source is not None:
        fields["title_source"] = source
    _write_core_meta(root, SID, fields)

    assert persistence.read_title(SID, user_only=True) == expected
    assert persistence.read_title(SID) == "Mine"


@needs_rename_core
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command", ["/session rename  My   title ", "/s rename  My   title "]
)
@pytest.mark.parametrize("session_id", [SID, "acp/unsafe id"])
async def test_rename_stores_user_title_and_announces_it(
    root, connection, command, session_id
):
    _seed(root, session_id)
    before = _snapshot(root)

    assert await commands.run(session_id, command) is None

    assert set(_snapshot(root)) == set(before)
    meta = json.loads(_core_meta_path(root, session_id).read_text(encoding="utf-8"))
    assert meta["title"] == "My title"
    assert meta["title_source"] == "user"
    [(message_sid, message), (update_sid, update)] = connection.updates
    assert message_sid == update_sid == session_id
    assert message.session_update == "agent_message_chunk"
    assert "My title" in message.content.text
    assert _is_session_info(update)
    assert update.session_update == "session_info_update"
    assert update.title == "My title"


@needs_rename_core
@pytest.mark.asyncio
async def test_taking_over_an_auto_title_announces_it(root, connection):
    _seed(root)
    _write_core_meta(root, SID, {"title": "Mine", "title_source": "auto"})

    await commands.run(SID, "/session rename Mine")

    titles = [u.title for _, u in connection.updates if _is_session_info(u)]
    assert titles == ["Mine"]
    assert persistence.read_title(SID, user_only=True) == "Mine"


@needs_rename_core
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command, seeded",
    [
        ("/session rename", True),
        ("/s rename", True),
        ("/session rename Mine", False),
    ],
)
async def test_rename_without_title_or_saved_session_changes_nothing(
    root, connection, command, seeded
):
    if seeded:
        _seed(root)
        # An existing user title must not be re-announced when nothing changed.
        _write_core_meta(root, SID, {"title": "Mine", "title_source": "user"})
    before = _snapshot(root)

    assert await commands.run(SID, command) is None

    assert _snapshot(root) == before
    [(message_sid, message)] = connection.updates
    assert message_sid == SID
    assert message.session_update == "agent_message_chunk"
    assert not any(_is_session_info(u) for _, u in connection.updates)


@needs_rename_core
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command, stub_result, expected_result, expected_texts",
    [
        ("  /session id  ", "Prompt text", "Prompt text", []),
        ("/help", None, None, ["Ran `/help`."]),
        ("/help", True, None, ["Ran `/help`."]),
    ],
)
async def test_other_commands_use_generic_handler(
    root, connection, command, stub_result, expected_result, expected_texts
):
    _seed(root)
    _write_core_meta(root, SID, {"title": "Mine", "title_source": "user"})
    with patch(HANDLER, return_value=stub_result) as handle:
        result = await commands.run(SID, command)

    handle.assert_called_once_with(command.strip())
    assert result == expected_result
    assert not any(_is_session_info(u) for _, u in connection.updates)
    assert [u.content.text for _, u in connection.updates] == expected_texts


@pytest.mark.asyncio
async def test_older_core_without_rename_api_falls_back_to_generic_handler(
    root, connection, monkeypatch
):
    _seed(root)
    command = "/session rename Mine"
    monkeypatch.setitem(sys.modules, SESSION_COMMANDS, types.SimpleNamespace())

    with patch(HANDLER, return_value=None) as handle:
        assert persistence.rename_session(SID, command) is False
        assert await commands.run(SID, command) is None

    assert handle.call_args_list[-1].args == (command,)
    assert "title" not in json.loads(_core_meta_path(root).read_text(encoding="utf-8"))
    assert [u.content.text for _, u in connection.updates] == [
        "Ran `/session rename Mine`."
    ]


@pytest.mark.asyncio
async def test_list_sessions_reports_titles_for_live_and_persisted(root):
    _seed(
        root,
        LIVE,
        cwd="/work/live",
        additional=["/work/shared"],
        timestamp="2026-08-16T12:00:00",
    )
    _write_core_meta(root, LIVE, {"title": "Live title"})
    _seed(root, SAVED, cwd="/work/saved", timestamp="2026-08-15T12:00:00")
    _write_core_meta(root, SAVED, {"title": "Saved title"})
    _seed(root, UNTITLED, cwd="/work/untitled", timestamp="2026-08-14T12:00:00")
    agent = CodePuppyAgent()
    agent._sessions[LIVE] = ACPSession(
        LIVE, None, cwd="/work/live", additional_directories=["/work/shared"]
    )

    response = await agent.list_sessions()

    assert [
        (s.session_id, s.cwd, s.additional_directories, s.title)
        for s in response.sessions
    ] == [
        (LIVE, "/work/live", ["/work/shared"], "Live title"),
        (SAVED, "/work/saved", None, "Saved title"),
        (UNTITLED, "/work/untitled", None, None),
    ]
    assert response.next_cursor is None
