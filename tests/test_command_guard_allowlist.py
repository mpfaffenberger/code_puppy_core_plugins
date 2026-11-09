"""Tests for the granular per-pattern command guard allowlist.

Covers:
* config helpers (normalize / parse / membership)
* the destructive command guard bypassing allowlisted patterns
* the force push guard bypassing allowlisted patterns
* both guards STILL blocking non-allowlisted patterns

The allowlist is shared across both guards (keyed off pattern_name), so a
single ``dangerous_command_guard_allow`` config entry can trust, e.g.,
``git reset --hard`` (destructive guard) and ``--force`` (force push guard)
while ``rm -rf /`` stays fully guarded.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from code_puppy import config
from code_puppy.tools.subagent_context import subagent_context
from code_puppy_core_plugins.destructive_command_guard import (
    register_callbacks as dcg,
)
from code_puppy_core_plugins.force_push_guard import register_callbacks as fpg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fake_get_value(store):
    """Build a config.get_value replacement backed by a plain dict."""
    return lambda key: store.get(key)


@pytest.fixture
def cfg(monkeypatch):
    """Route config reads through a per-test dict."""
    store: dict = {}
    monkeypatch.setattr(config, "get_value", _fake_get_value(store))
    return store


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


class TestNormalize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("git reset --hard", "git reset --hard"),
            ("  Git   Reset   --Hard  ", "git reset --hard"),
            ("GIT PUSH --FORCE", "git push --force"),
            ("--Force", "--force"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_normalize(self, raw, expected):
        assert config.normalize_guard_pattern_name(raw) == expected


class TestAllowlistParsing:
    def test_empty_when_unset(self, cfg):
        assert config.get_dangerous_command_guard_allowlist() == set()

    def test_splits_and_normalizes(self, cfg):
        cfg["dangerous_command_guard_allow"] = (
            "git reset --hard, GIT PUSH --force ,, --Force"
        )
        assert config.get_dangerous_command_guard_allowlist() == {
            "git reset --hard",
            "git push --force",
            "--force",
        }

    def test_membership(self, cfg):
        cfg["dangerous_command_guard_allow"] = "Git Reset --Hard"
        assert config.is_dangerous_command_allowlisted("git reset --hard") is True
        assert config.is_dangerous_command_allowlisted("rm -rf /") is False
        assert config.is_dangerous_command_allowlisted("") is False


# ---------------------------------------------------------------------------
# Destructive command guard callback
# ---------------------------------------------------------------------------


class TestDestructiveGuardAllowlist:
    def test_allowlisted_pattern_is_waved_through(self, cfg, monkeypatch):
        cfg["dangerous_command_guard_allow"] = "git reset --hard"
        monkeypatch.setattr(dcg, "_is_interactive", lambda: False)

        result = asyncio.run(
            dcg.destructive_command_guard_callback(
                None, "cd repo && git reset --hard origin/main"
            )
        )
        assert result is None  # allowed, no prompt/block

    def test_non_allowlisted_pattern_still_blocked(self, cfg, monkeypatch):
        cfg["dangerous_command_guard_allow"] = "git reset --hard"
        monkeypatch.setattr(dcg, "_is_interactive", lambda: False)

        result = asyncio.run(
            dcg.destructive_command_guard_callback(None, "cd tmp && rm -rf /")
        )
        assert result is not None
        assert result["blocked"] is True

    def test_legacy_disable_flag_still_wins(self, cfg, monkeypatch):
        cfg["disable_dangerous_command_guard"] = "true"
        monkeypatch.setattr(dcg, "_is_interactive", lambda: False)

        result = asyncio.run(
            dcg.destructive_command_guard_callback(None, "cd tmp && rm -rf /")
        )
        assert result is None  # global kill-switch bypasses everything


# ---------------------------------------------------------------------------
# Force push guard callback (shared allowlist)
# ---------------------------------------------------------------------------


class TestForcePushGuardAllowlist:
    def test_allowlisted_force_push_is_waved_through(self, cfg, monkeypatch):
        # Same shared config key covers the force-push guard's pattern names.
        cfg["dangerous_command_guard_allow"] = "git reset --hard, --force"
        monkeypatch.setattr(fpg, "_is_interactive", lambda: False)

        result = asyncio.run(
            fpg.force_push_guard_callback(None, "git push --force origin develop")
        )
        assert result is None

    def test_non_allowlisted_force_push_still_blocked(self, cfg, monkeypatch):
        cfg["dangerous_command_guard_allow"] = "--force"  # only long flag trusted
        monkeypatch.setattr(fpg, "_is_interactive", lambda: False)

        result = asyncio.run(
            fpg.force_push_guard_callback(None, "git push -f origin develop")
        )
        assert result is not None
        assert result["blocked"] is True


# ---------------------------------------------------------------------------
# Sub-agents can never answer an approval prompt
# ---------------------------------------------------------------------------

GUARDS = [
    (dcg, "destructive_command_guard_callback", "cd tmp && rm -rf /"),
    (fpg, "force_push_guard_callback", "git push --force origin develop"),
]


@pytest.fixture
def tty_attached(monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))


@pytest.mark.usefixtures("cfg", "tty_attached")
class TestGuardsAndSubagents:
    @pytest.mark.parametrize("guard, callback, command", GUARDS)
    def test_subagent_is_blocked_without_a_prompt(
        self, monkeypatch, guard, callback, command
    ):
        prompt = AsyncMock(return_value=None)
        monkeypatch.setattr(guard, "_prompt_user_approval", prompt)

        with subagent_context("tester"):
            result = asyncio.run(getattr(guard, callback)(None, command))

        assert result["blocked"] is True
        prompt.assert_not_awaited()

    @pytest.mark.parametrize("guard, callback, command", GUARDS)
    def test_main_agent_on_a_tty_is_still_prompted(
        self, monkeypatch, guard, callback, command
    ):
        prompt = AsyncMock(return_value=None)
        monkeypatch.setattr(guard, "_prompt_user_approval", prompt)

        result = asyncio.run(getattr(guard, callback)(None, command))

        assert result is None
        prompt.assert_awaited_once()
