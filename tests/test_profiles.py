"""Profiles isolate the real config APIs, not a parallel settings implementation."""

import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from code_puppy import config as core
from code_puppy.i18n import add_catalog_dir, get_locale, set_locale, t
from code_puppy_core_plugins.profiles import config, register_callbacks as hooks
from code_puppy_core_plugins.profiles.menu import ProfileMenu


@pytest.fixture(autouse=True)
def isolated_profiles(monkeypatch):
    monkeypatch.setattr(config, "_active", "default")
    monkeypatch.setattr(core, "_SESSION_MODEL", None)
    monkeypatch.setattr(core, "_CONFIG_CACHE", None)
    add_catalog_dir(Path(hooks.__file__).parent / "locales")
    core.set_config_value("owner_name", "Test owner")


def test_default_preserves_existing_config():
    before = Path(core.CONFIG_FILE).read_bytes()
    config.activate_profile("default")
    assert config.list_profiles() == ["default"]
    assert core.get_value("owner_name") == "Test owner"
    assert Path(core.CONFIG_FILE).read_bytes() == before
    assert not (Path(core.CONFIG_DIR) / "profiles").exists()


def test_credentials_shared_not_copied(monkeypatch):
    from code_puppy import shared_credentials

    store = {}
    monkeypatch.setattr(shared_credentials.secret_store, "get_secret", store.get)
    monkeypatch.setattr(
        shared_credentials.secret_store, "set_secret", store.__setitem__
    )
    with Path(core.CONFIG_FILE).open("a") as file:
        file.write("\nopenai_api_key = legacy-test-key\n")
    config.create_profile("web")
    assert "legacy-test-key" not in config.profile_path("web").read_text()
    assert "legacy-test-key" not in config.profile_path("default").read_text()
    config.activate_profile("web")
    assert core.get_api_key("OPENAI_API_KEY") == "legacy-test-key"
    core.set_config_value("openai_api_key", "rotated-test-key")
    config.activate_profile("default")
    assert core.get_api_key("OPENAI_API_KEY") == "rotated-test-key"


def test_snapshot_pins_and_settings_are_isolated():
    core.set_agent_pinned_model("reviewer", "model-a")
    core.set_model_setting("model-a", "max_tokens", 1234)
    config.create_profile("web")
    config.activate_profile("web")
    assert core.get_agent_pinned_model("reviewer") == "model-a"
    assert core.get_model_setting("model-a", "max_tokens") == 1234
    core.set_agent_pinned_model("reviewer", "model-b")
    core.set_model_setting("model-a", "max_tokens", 5678)
    config.activate_profile("default")
    assert core.get_agent_pinned_model("reviewer") == "model-a"
    assert core.get_model_setting("model-a", "max_tokens") == 1234
    config.activate_profile("web")
    assert core.get_agent_pinned_model("reviewer") == "model-b"


@pytest.mark.parametrize(
    "name", ["../oops", "/tmp/oops", "", "UPPER", "a.b", "x" * 65, "con", "lpt1"]
)
def test_bad_names_rejected(name):
    with pytest.raises(ValueError):
        config.create_profile(name)
    assert config.active_profile() == "default"


def test_no_overwrites_or_implicit_creation():
    config.create_profile("web")
    before = config.profile_path("web").read_bytes()
    for name in ("web", "default"):
        with pytest.raises(ValueError):
            config.create_profile(name)
    with pytest.raises(ValueError):
        config.activate_profile("missing")
    assert config.profile_path("web").read_bytes() == before
    assert config.active_profile() == "default"


def test_symlink_profile_rejected(tmp_path):
    root = Path(core.CONFIG_DIR) / "profiles"
    root.mkdir()
    (root / "alias").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        config.create_profile("alias")
    assert config.list_profiles() == ["default"]


def test_activation_clears_cached_model(monkeypatch):
    config.create_profile("web")
    monkeypatch.setattr(core, "_SESSION_MODEL", "old-model")
    config.activate_profile("web")
    assert core._SESSION_MODEL is None


def test_cli_selection_and_missing_profile(capsys):
    parser = argparse.ArgumentParser()
    hooks.register_cli_args(parser)
    assert hooks.handle_cli_args(parser.parse_args([])) is None
    config.create_profile("web")
    assert hooks.handle_cli_args(parser.parse_args(["--profile", "web"])) is None
    assert config.active_profile() == "web"
    assert hooks.handle_cli_args(parser.parse_args([])) is None
    assert config.active_profile() == "default"
    assert hooks.handle_cli_args(parser.parse_args(["--profile", "missing"])) == {
        "handled": True,
        "exit_code": 2,
    }
    assert "missing" in capsys.readouterr().err


def test_switch_rebuilds_agent_and_rolls_back(monkeypatch):
    from code_puppy.agents import agent_manager

    config.create_profile("web")
    monkeypatch.setattr(agent_manager, "get_current_agent_name", lambda: "reviewer")
    rebuild = Mock()
    monkeypatch.setattr(agent_manager, "set_current_agent", rebuild)
    hooks.switch_profile("web")
    rebuild.assert_called_once_with("reviewer")
    rebuild.side_effect = RuntimeError("oops")
    with pytest.raises(RuntimeError):
        hooks.switch_profile("default")
    assert config.active_profile() == "web"


def test_command_dispatch(monkeypatch):
    from code_puppy import messaging

    info, error = Mock(), Mock()
    monkeypatch.setattr(messaging, "emit_info", info)
    monkeypatch.setattr(messaging, "emit_error", error)
    assert hooks.custom_command("/other", "other") is None
    assert hooks.custom_command("/profile create web", "profile") is True
    assert "web" in config.list_profiles()
    hooks.custom_command("/profile list", "profile")
    assert "web" in info.call_args.args[0]
    hooks.custom_command('/profile "', "profile")
    error.assert_called_once()


def test_menu_create_activate_cancel(monkeypatch):
    monkeypatch.setattr(hooks, "switch_profile", config.activate_profile)
    menu = ProfileMenu()
    menu.on_key("c")
    for key in "web":
        menu.on_key(key)
    menu.on_key("enter")
    assert not menu.creating
    assert config.active_profile() == "default"
    menu.on_key("enter")
    assert config.active_profile() == "web"
    assert "web" in "\n".join(menu.render())
    menu.on_key("c")
    assert menu.on_key("escape") is False
    assert menu.on_key("ctrl-c") is True


def test_process_selection_does_not_leak():
    config.create_profile("web")
    config.activate_profile("web")
    core.set_agent_pinned_model("reviewer", "parent-model")
    script = """
import sys
from code_puppy import config as core
from code_puppy_core_plugins.profiles import config
core.CONFIG_DIR = sys.argv[1]
config.activate_profile(sys.argv[2])
core.set_agent_pinned_model('reviewer', 'child-model')
"""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    subprocess.run(
        [sys.executable, "-c", script, str(core.CONFIG_DIR), "default"],
        env=env,
        check=True,
        timeout=30,
    )
    assert config.active_profile() == "web"
    assert core.get_agent_pinned_model("reviewer") == "parent-model"
    config.activate_profile("default")
    assert core.get_agent_pinned_model("reviewer") == "child-model"


def test_menu_event_loop(monkeypatch):
    from code_puppy_core_plugins.termflow_tui import FragmentTUI

    monkeypatch.setattr(hooks, "switch_profile", config.activate_profile)
    keys = iter(["c", "w", "e", "b", "enter", "enter", "escape"])
    output = io.StringIO()
    menu = ProfileMenu()
    FragmentTUI(
        menu.render,
        menu.on_key,
        key_source=lambda: next(keys),
        output=output,
        size=lambda: (100, 24),
        use_alt_screen=False,
    ).run()
    assert config.active_profile() == "web"
    assert "web" in output.getvalue()


def test_all_catalog_keys_pseudolocalize():
    catalog = json.loads(
        (Path(hooks.__file__).parent / "locales/en-US.json").read_text()
    )
    previous = get_locale()
    try:
        set_locale("en-XA")
        for key in catalog:
            text = t(key)
            assert text != key
            assert text.startswith("⟦") and text.endswith("⟧")
    finally:
        set_locale(previous)
