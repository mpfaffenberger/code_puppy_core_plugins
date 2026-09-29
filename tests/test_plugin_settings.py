"""Plugins declare their `/set` settings through core's register_settings hook."""

from __future__ import annotations

import importlib

import pytest
from termflow.tui.completion import Document

from code_puppy.command_line import completers
from code_puppy.command_line.set_menu_settings import iter_curated_settings
from code_puppy.command_line.set_menu_values import is_sensitive_key
from code_puppy.config import get_config_keys, set_config_value
from code_puppy_core_plugins import plugin_settings

# plugin module -> {key: menu category it should land in}
EXPECTED = {
    "jev_grep": {
        "smart_grep": "Smart Grep",
        "smart_grep_model": "Smart Grep",
        "smart_grep_threshold": "Smart Grep",
        "typesafe_api_key": "API Keys",
    },
    "wiggum": {"goal_max_iterations": "Goal"},
    "timestamp_heartbeat": {"timestamp_heartbeat_interval": "Behavior"},
    "auto_continue": {"auto_continue_model": "Model"},
    "dbos_durable_exec": {"enable_dbos": "Features"},
    "frontend_emitter": {
        "frontend_emitter_enabled": "Features",
        "frontend_emitter_max_recent_events": "Features",
        "frontend_emitter_queue_size": "Features",
    },
}


@pytest.fixture(autouse=True)
def _loaded_plugins():
    for name in EXPECTED:
        importlib.import_module(f"code_puppy_core_plugins.{name}.register_callbacks")
    completers._config_keys_cache.clear()
    yield
    completers._config_keys_cache.clear()


def _complete(text: str) -> list[str]:
    document = Document(text=text, cursor_position=len(text))
    return [c.text for c in completers.SetCompleter().get_completions(document, None)]


def test_old_core_without_the_hook_is_tolerated(monkeypatch):
    def reject(phase, func):
        raise ValueError(f"Unsupported phase: {phase}")

    monkeypatch.setattr(plugin_settings, "register_callback", reject)
    plugin_settings.register_settings(object())  # must not raise


@pytest.mark.parametrize("plugin", sorted(EXPECTED))
def test_plugin_settings_reach_menu_and_completion(plugin):
    placed = {s.key: c.name for c, s in iter_curated_settings()}
    keys = set(get_config_keys())
    for key, category in EXPECTED[plugin].items():
        assert placed.get(key) == category
        assert key in keys


def test_smart_grep_completes_once_before_and_after_saving():
    assert _complete("/set smart_grep").count("smart_grep = ") == 1
    set_config_value("smart_grep", "on")
    completers._config_keys_cache.clear()
    texts = _complete("/set smart_grep")
    assert [t for t in texts if t.startswith("smart_grep =")] == ["smart_grep = on"]


def test_typesafe_key_is_masked_and_never_echoed():
    set_config_value("typesafe_api_key", "ts-super-secret")
    assert is_sensitive_key("typesafe_api_key")
    assert _complete("/set typesafe") == ["typesafe_api_key = "]
