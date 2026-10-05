"""Installation guidance and plugin-owned translations; no desktop access."""

import json
import sys
from importlib.resources import files
from types import SimpleNamespace

import pytest
from code_puppy.i18n import register_plugin_catalog
from code_puppy.i18n import translate
from code_puppy.plugins import _plugin_loading_context

from code_puppy_core_plugins.computer_use import commands, register_callbacks
from code_puppy_core_plugins.computer_use.backend_types import ComputerUseError
from code_puppy_core_plugins.computer_use.windows_runtime import WindowsRuntime


@pytest.mark.parametrize("locale", ["en-US", "es", "fr-CA", "de"])
def test_plugin_catalog_translates_windows_messages(monkeypatch, locale):
    resource = files("code_puppy_core_plugins.computer_use").joinpath("locales")
    with _plugin_loading_context("computer_use"):
        assert register_plugin_catalog(resource)
    monkeypatch.setattr(translate, "_translator", translate.Translator(locale))
    source_locale = "en-US" if locale == "de" else locale
    expected = json.loads(resource.joinpath(f"{source_locale}.json").read_text("utf-8"))
    assert commands.command_help() == [
        ("computer-use", expected["plugin.computer-use.command.help"])
    ]
    errors = []
    monkeypatch.setattr(commands, "emit_error", errors.append)
    assert commands.handle_command("/computer-use deny", "computer-use") is True
    assert errors == [expected["plugin.computer-use.command.policy_usage"]]
    messages = []
    monkeypatch.setattr(register_callbacks, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(register_callbacks.policy_store, "is_enabled", lambda: False)
    monkeypatch.setattr(register_callbacks, "emit_info", messages.append)
    register_callbacks._startup()
    assert messages == [expected["plugin.computer-use.startup.windows_opt_in"]]
    assert not messages[0].startswith("plugin.")


def test_missing_windows_dependencies_names_plugin_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "comtypes", None)
    runtime = WindowsRuntime()
    try:
        with pytest.raises(ComputerUseError) as error:
            runtime._initialize()
        assert "code-puppy-core-plugins[computer-use]" in str(error.value)
        assert "'code-puppy[computer-use]'" not in str(error.value)
    finally:
        runtime._executor.shutdown()
