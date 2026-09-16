"""Shared fixtures for the browser-harness plugin tests."""

from __future__ import annotations

import pytest

from code_puppy_core_plugins.browser_harness import cli
from code_puppy_core_plugins.browser_harness import policy
from code_puppy_core_plugins.browser_harness.policy import SettingsStore


@pytest.fixture(autouse=True)
def clean_harness_env(monkeypatch):
    """Never let the developer's real harness config leak into a test."""
    for name in ("BU_CDP_URL", "BU_CDP_WS", "BU_NAME", cli.EXECUTABLE_ENV_VAR):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _quiet_registry_refresh(monkeypatch):
    """Stop /browser enable from mutating Code Puppy's real tool registry."""
    from code_puppy import tools as core_tools

    monkeypatch.setattr(core_tools, "get_available_tool_names", lambda: [])


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the whole plugin at one throwaway settings file."""
    isolated = SettingsStore(tmp_path / "browser_harness_policy.json")
    monkeypatch.setattr(policy, "settings_store", isolated)
    return isolated


@pytest.fixture
def harness_bin(tmp_path, monkeypatch):
    """Pretend ``browser-harness`` is installed, via the override env var."""
    binary = tmp_path / "browser-harness"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv(cli.EXECUTABLE_ENV_VAR, str(binary))
    return binary
