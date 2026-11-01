"""CodePuppyAgent wiring for the theme catalog: ext method, session _meta, watcher lifecycle."""

from __future__ import annotations

import asyncio
import copy
import logging

import pytest
from acp import RequestError
from code_puppy.config import set_config_value

from code_puppy_core_plugins.acp import (
    commands,
    io_delegation,
    permissions,
    state,
    themes,
)
from code_puppy_core_plugins.acp.agent import CodePuppyAgent
from code_puppy_core_plugins.theme import (
    catalog as catalog_module,
    content_styles,
    osc_palette,
    rich_themes,
)
from code_puppy_core_plugins.theme.catalog import ACTIVE_THEME_CONFIG_KEY, build_catalog
from code_puppy_core_plugins.theme.themes import CURATED_THEMES, MENU


class _Connection:
    def __init__(self) -> None:
        self.updates: list = []
        self.notifications: list = []

    async def session_update(self, session_id, update) -> None:
        self.updates.append((session_id, update))

    async def ext_notification(self, method, params) -> None:
        self.notifications.append((method, params))


class _FakeAgent:
    def get_message_history(self) -> list:
        return []

    def set_message_history(self, history) -> None:
        pass


@pytest.fixture
def _restore_rich_state():
    yield
    content_styles.restore_defaults()
    rich_themes.restore()


@pytest.fixture(autouse=True)
def _restore_process_state(monkeypatch):
    previous = (state.get_connection(), state.get_loop())
    state.set_connection(None, None)
    monkeypatch.setattr(CodePuppyAgent, "_new_agent", staticmethod(_FakeAgent))
    yield
    state.set_connection(*previous)
    permissions.uninstall()
    io_delegation.uninstall()


def _active_theme(monkeypatch, value):
    monkeypatch.setattr(themes, "read_active_theme_id", lambda: value)


@pytest.mark.parametrize("active", [None, "ocean"])
async def test_list_answers_before_any_session(monkeypatch, active):
    _active_theme(monkeypatch, active)
    agent = CodePuppyAgent()

    result = await agent.ext_method(themes.EXT_LIST, {})

    assert result == {"schemaVersion": 1, "active": active, "themes": build_catalog()}


@pytest.mark.parametrize("method", ["code_puppy/themes/set", "other/thing"])
async def test_unknown_ext_method_is_method_not_found(method):
    agent = CodePuppyAgent()

    with pytest.raises(RequestError) as raised:
        await agent.ext_method(method, {})

    assert raised.value.code == -32601
    assert raised.value.data == {"method": "_" + method}


@pytest.mark.parametrize("active", [None, "ocean"])
@pytest.mark.parametrize("method", ["new_session", "load_session", "resume_session"])
async def test_session_responses_carry_active_theme_meta(monkeypatch, method, active):
    _active_theme(monkeypatch, active)
    agent = CodePuppyAgent()

    response = await getattr(agent, method)(cwd="/work", session_id="sess_saved")

    assert response.field_meta == {themes.META_KEY: {"active": active}}


async def test_new_cli_theme_is_listed_and_reported_active_over_acp(monkeypatch):
    theme = copy.deepcopy(CURATED_THEMES["ocean"])
    theme["label"] = "Zz New"
    monkeypatch.setitem(CURATED_THEMES, "zz-new", theme)
    menu = [*MENU]
    menu.insert([name for name, _ in menu].index("surprise"), ("zz-new", theme))
    monkeypatch.setattr(catalog_module, "MENU", menu)
    set_config_value(ACTIVE_THEME_CONFIG_KEY, "zz-new")
    agent = CodePuppyAgent()

    listed = await agent.ext_method(themes.EXT_LIST, {})
    response = await agent.new_session(cwd="/work", session_id="sess_new")

    assert listed == {"schemaVersion": 1, "active": "zz-new", "themes": build_catalog()}
    assert "zz-new" in [entry["id"] for entry in listed["themes"]]
    assert response.field_meta == {themes.META_KEY: {"active": "zz-new"}}


async def test_on_connect_starts_watcher_and_shutdown_stops_it():
    agent = CodePuppyAgent()

    agent.on_connect(_Connection())
    task = agent._theme_watcher._task
    assert task is not None and not task.done()
    assert agent._theme_watcher._poll_seconds == themes.POLL_SECONDS
    assert agent._theme_watcher._poll_seconds == 2.0

    agent.shutdown()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_watcher_start_failure_never_blocks_connect(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="code_puppy_core_plugins.acp.agent")
    agent = CodePuppyAgent()

    def boom() -> None:
        raise RuntimeError("secret detail that must not be logged")

    monkeypatch.setattr(agent._theme_watcher, "start", boom)

    agent.on_connect(_Connection())

    assert state.get_connection() is not None
    assert "RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text
    agent.shutdown()


async def test_theme_command_pushes_one_changed_notification(
    monkeypatch, _restore_rich_state
):
    # Importing registers the /theme command handler with the command registry.
    from code_puppy_core_plugins.theme import register_callbacks  # noqa: F401

    # The real palette would write OSC escapes to the test terminal.
    monkeypatch.setattr(osc_palette, "apply_palette", lambda *a, **k: None)
    monkeypatch.setattr(osc_palette, "reset_palette", lambda *a, **k: None)
    connection = _Connection()
    state.set_connection(connection, asyncio.get_running_loop())
    set_config_value(ACTIVE_THEME_CONFIG_KEY, "ocean")
    watcher = themes.ThemeWatcher()
    await watcher.poll_once()

    await commands.run("sess_theme", "/theme forest")
    await watcher.poll_once()

    assert connection.notifications == [
        (themes.EXT_CHANGED, {"active": "forest"}),
    ]
