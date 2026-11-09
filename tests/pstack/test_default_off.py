"""pstack ships in the bundle dark: nothing reaches the agent until opted in."""

from __future__ import annotations

from importlib.metadata import entry_points

import pytest
from code_puppy import callbacks
from code_puppy.command_line.set_menu_schema import SettingsCategory
from code_puppy.config import reset_value, set_value

from code_puppy_core_plugins.pstack import register_callbacks as plugin
from code_puppy_core_plugins.pstack.commands import handle_command
from code_puppy_core_plugins.pstack.config import ENABLED_KEY, is_enabled


@pytest.fixture
def pstack_unset() -> None:
    """A fresh install: the key was never written."""
    reset_value(ENABLED_KEY)


@pytest.mark.usefixtures("pstack_unset")
def test_fresh_install_is_off_and_adds_nothing() -> None:
    assert not is_enabled()
    assert plugin.skill_registrations() == []
    assert plugin.prompt_addition() is None
    assert plugin.agents() == []
    assert plugin.agent_tools("code-puppy") == []
    # The full pack still renders; only the hooks are gated.
    assert len(plugin.all_skill_registrations()) == 33


@pytest.mark.usefixtures("pstack_unset")
def test_bare_command_explains_how_to_turn_it_on() -> None:
    text = handle_command("/pstack", "pstack") or ""
    assert "is off" in text
    assert "/pstack on" in text
    assert f"/set {ENABLED_KEY}=true" in text


@pytest.mark.usefixtures("pstack_unset")
def test_on_and_off_flip_every_hook() -> None:
    assert "pstack is on" in (handle_command("/pstack on", "pstack") or "")
    assert is_enabled()
    assert {e["name"] for e in plugin.skill_registrations()} >= {"poteto-mode"}
    assert plugin.prompt_addition()
    assert [a["name"] for a in plugin.agents()]
    assert plugin.agent_tools("code-puppy")

    assert "pstack is off" in (handle_command("/pstack off", "pstack") or "")
    assert not is_enabled()
    assert plugin.skill_registrations() == []
    assert plugin.prompt_addition() is None


@pytest.mark.parametrize("value", ["true", "1", "on", "yes"])
def test_set_command_values_turn_it_on(value: str) -> None:
    set_value(ENABLED_KEY, value)
    assert is_enabled()


def test_unknown_subcommand_shows_usage() -> None:
    assert handle_command("/pstack maybe", "pstack") == "Usage: /pstack [on|off]"


def test_setting_is_offered_in_the_set_menu() -> None:
    categories = [
        category
        for result in callbacks.on_register_settings()
        for category in (result if isinstance(result, list) else [result])
        if isinstance(category, SettingsCategory)
    ]
    keys = {setting.key for category in categories for setting in category.settings}
    assert ENABLED_KEY in keys


def test_bundle_advertises_the_entry_point() -> None:
    points = entry_points(group="code_puppy.plugins", name="pstack")
    assert [p.value for p in points] == [
        "code_puppy_core_plugins.pstack.register_callbacks"
    ]
