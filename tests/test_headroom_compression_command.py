"""Tests for the /headroom command dispatch."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from code_puppy_core_plugins.headroom_compression import command as command_module
from code_puppy_core_plugins.headroom_compression import proxy
from code_puppy_core_plugins.headroom_compression.command import (
    get_headroom_command_help,
    handle_headroom_command,
)


def test_non_headroom_command_returns_none():
    assert handle_headroom_command("/other", "other") is None


def test_enable_requires_a_url():
    with patch(
        "code_puppy_core_plugins.headroom_compression.command.emit_error"
    ) as mock_error:
        result = handle_headroom_command("/headroom enable", "headroom")
    assert result is True
    mock_error.assert_called_once()


def test_enable_with_url_calls_config_and_starts_proxy():
    with (
        patch(
            "code_puppy_core_plugins.headroom_compression.command.config.enable"
        ) as mock_enable,
        patch(
            "code_puppy_core_plugins.headroom_compression.command.proxy.start_proxy",
            return_value=True,
        ) as mock_start,
        patch("code_puppy_core_plugins.headroom_compression.command.emit_success"),
    ):
        handle_headroom_command(
            "/headroom enable https://example.com/anthropic", "headroom"
        )
    mock_enable.assert_called_once_with("https://example.com/anthropic")
    mock_start.assert_called_once_with("https://example.com/anthropic")


def test_enable_warns_when_proxy_fails_to_start():
    with (
        patch("code_puppy_core_plugins.headroom_compression.command.config.enable"),
        patch(
            "code_puppy_core_plugins.headroom_compression.command.proxy.start_proxy",
            return_value=False,
        ),
        patch(
            "code_puppy_core_plugins.headroom_compression.command.emit_warning"
        ) as mock_warning,
    ):
        handle_headroom_command(
            "/headroom enable https://example.com/anthropic", "headroom"
        )
    mock_warning.assert_called_once()


def test_disable_stops_proxy():
    with (
        patch(
            "code_puppy_core_plugins.headroom_compression.command.config.disable"
        ) as mock_disable,
        patch(
            "code_puppy_core_plugins.headroom_compression.command.proxy.stop_proxy"
        ) as mock_stop,
        patch("code_puppy_core_plugins.headroom_compression.command.emit_info"),
    ):
        handle_headroom_command("/headroom disable", "headroom")
    mock_disable.assert_called_once()
    mock_stop.assert_called_once()


def test_restart_reports_success():
    with (
        patch(
            "code_puppy_core_plugins.headroom_compression.command.proxy.restart_proxy",
            return_value=True,
        ),
        patch(
            "code_puppy_core_plugins.headroom_compression.command.emit_success"
        ) as mock_success,
    ):
        handle_headroom_command("/headroom restart", "headroom")
    mock_success.assert_called_once()


def test_restart_reports_failure():
    with (
        patch(
            "code_puppy_core_plugins.headroom_compression.command.proxy.restart_proxy",
            return_value=False,
        ),
        patch(
            "code_puppy_core_plugins.headroom_compression.command.emit_error"
        ) as mock_error,
    ):
        handle_headroom_command("/headroom restart", "headroom")
    mock_error.assert_called_once()


def test_status_defaults_when_no_subcommand():
    with patch(
        "code_puppy_core_plugins.headroom_compression.command._show_status"
    ) as mock_status:
        handle_headroom_command("/headroom", "headroom")
    mock_status.assert_called_once()


def test_unrecognized_subcommand_passes_through_to_headroom_cli():
    with (
        patch(
            "code_puppy_core_plugins.headroom_compression.command.proxy._headroom_bin",
            return_value="/fake/headroom",
        ),
        patch(
            "code_puppy_core_plugins.headroom_compression.command.subprocess.run"
        ) as mock_run,
    ):
        result = handle_headroom_command("/headroom doctor", "headroom")
    assert result is True
    mock_run.assert_called_once_with(["/fake/headroom", "doctor"])


def test_safe_but_not_in_session_useful_commands_are_still_rejected():
    """telemetry and rollout are individually verified read-only/safe
    against their own --help (see module docstring) -- but safety alone
    isn't the bar. Neither is something a user would reach for without
    leaving a code-puppy session (one-off audit of headroom's telemetry
    disclosure, or headroom's own internal feature-rollout policy), so
    both stay excluded even though nothing would go wrong if forwarded.
    """
    for cmd in ("telemetry", "rollout"):
        with (
            patch(
                "code_puppy_core_plugins.headroom_compression.command.subprocess.run"
            ) as mock_run,
            patch(
                "code_puppy_core_plugins.headroom_compression.command.emit_warning"
            ) as mock_warning,
        ):
            result = handle_headroom_command(f"/headroom {cmd}", "headroom")
        assert result is True
        mock_run.assert_not_called()
        mock_warning.assert_called_once()


def test_passthrough_forwards_extra_args_for_allowlisted_command():
    with (
        patch(
            "code_puppy_core_plugins.headroom_compression.command.proxy._headroom_bin",
            return_value="/fake/headroom",
        ),
        patch(
            "code_puppy_core_plugins.headroom_compression.command.subprocess.run"
        ) as mock_run,
    ):
        handle_headroom_command("/headroom perf --since 1h", "headroom")
    mock_run.assert_called_once_with(["/fake/headroom", "perf", "--since", "1h"])


def test_non_allowlisted_subcommand_is_rejected_not_forwarded():
    """Regression guard: headroom ships subcommands that start their own
    untracked proxy/server (proxy, mcp, deploy), mutate other tools'
    configs (wrap/unwrap/install/init), mutate the headroom binary itself
    (update), make real LLM calls or run benchmark suites (learn, evals),
    handle privacy-sensitive raw traffic (capture), only audit other
    tools' transcript formats (audit-reads), merge/recover on-disk state
    (recover), or are moot under --stateless (memory, inspect). None of
    these may ever reach subprocess.run just because a user typed them --
    only the explicit allowlist may.
    """
    for dangerous in (
        "proxy",
        "mcp",
        "deploy",
        "update",
        "wrap",
        "install",
        "memory",
        "learn",
        "evals",
        "capture",
        "audit-reads",
        "recover",
        "inspect",
        "agent-savings",
        "telemetry",
        "rollout",
    ):
        with (
            patch(
                "code_puppy_core_plugins.headroom_compression.command.subprocess.run"
            ) as mock_run,
            patch(
                "code_puppy_core_plugins.headroom_compression.command.emit_warning"
            ) as mock_warning,
        ):
            result = handle_headroom_command(f"/headroom {dangerous}", "headroom")
        assert result is True
        mock_run.assert_not_called()
        mock_warning.assert_called_once()


def test_passthrough_warns_when_headroom_not_installed():
    with (
        patch(
            "code_puppy_core_plugins.headroom_compression.command.proxy._headroom_bin",
            return_value=None,
        ),
        patch(
            "code_puppy_core_plugins.headroom_compression.command.subprocess.run"
        ) as mock_run,
        patch(
            "code_puppy_core_plugins.headroom_compression.command.emit_warning"
        ) as mock_warning,
    ):
        result = handle_headroom_command("/headroom doctor", "headroom")
    assert result is True
    mock_warning.assert_called_once()
    mock_run.assert_not_called()


def test_get_headroom_command_help_advertises_command():
    entries = get_headroom_command_help()
    assert entries[0][0] == "headroom"


class TestAllowlistDriftDetection:
    """Dynamically discovers headroom's *real* subcommand list (by parsing
    `headroom --help`) and cross-checks it against our static classification.

    This does NOT auto-approve anything -- see the module docstring for why
    that would be a bad idea (a command can look safe by name and still
    hide a destructive flag, as `agent-savings` did). It only tells us WHEN
    a human needs to go read a new command's --help and make a deliberate
    call, and catches the case where headroom renames/removes a command we
    already allowlisted (which would otherwise fail silently -- the
    passthrough would just warn 'unsupported subcommand' forever with no
    one noticing why).

    Skipped entirely if headroom isn't installed in the current
    environment -- this is meant to run wherever a real headroom binary is
    available (e.g. local dev), not to require headroom-ai (with its heavy
    torch/transformers deps) as a permanent CI dependency just for this.
    """

    @pytest.fixture
    def headroom_bin(self):
        bin_path = proxy._headroom_bin()
        if not bin_path:
            pytest.skip("headroom not installed in this environment")
        return bin_path

    def test_no_stale_allowlist_entries(self, headroom_bin):
        stale = command_module.find_stale_allowlist_entries(headroom_bin)
        if stale is None:
            pytest.skip("could not parse headroom --help output")
        assert not stale, (
            f"Allowlisted command(s) {sorted(stale)} no longer exist in "
            "the installed headroom CLI -- headroom likely renamed or "
            "removed them. Update _PASSTHROUGH_ALLOWLIST."
        )

    def test_no_unreviewed_new_subcommands(self, headroom_bin):
        unreviewed = command_module.find_unreviewed_headroom_subcommands(
            headroom_bin
        )
        if unreviewed is None:
            pytest.skip("could not parse headroom --help output")
        assert not unreviewed, (
            f"headroom now ships {sorted(unreviewed)}, not yet classified "
            "here. Run `headroom <cmd> --help` for each, then add it to "
            "either _PASSTHROUGH_ALLOWLIST (if genuinely read-only with no "
            "destructive flag reachable through forwarded args) or "
            "_KNOWN_EXCLUDED (with a one-line reason) in command.py. Never "
            "add to the allowlist without reading its --help first."
        )
