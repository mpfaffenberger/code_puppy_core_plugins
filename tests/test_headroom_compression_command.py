"""Tests for the /headroom command dispatch."""

from __future__ import annotations

from unittest.mock import patch

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


def test_passthrough_forwards_extra_args():
    with (
        patch(
            "code_puppy_core_plugins.headroom_compression.command.proxy._headroom_bin",
            return_value="/fake/headroom",
        ),
        patch(
            "code_puppy_core_plugins.headroom_compression.command.subprocess.run"
        ) as mock_run,
    ):
        handle_headroom_command("/headroom memory list", "headroom")
    mock_run.assert_called_once_with(["/fake/headroom", "memory", "list"])


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
