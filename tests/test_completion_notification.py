"""Tests for the opt-in completion-notification plugin."""

from __future__ import annotations

from unittest.mock import Mock

from code_puppy_core_plugins.completion_notification import config, notifier
from code_puppy_core_plugins.completion_notification import (
    register_callbacks as callbacks,
)


def test_config_defaults_to_disabled_and_empty_sound(monkeypatch):
    monkeypatch.setattr(config, "get_value", lambda _key: None)

    assert config.is_enabled() is False
    assert config.get_sound() == ""


def test_config_accepts_truthy_values(monkeypatch):
    monkeypatch.setattr(
        config,
        "get_value",
        lambda key: " yes " if key == config.KEY_ENABLED else " Frog ",
    )

    assert config.is_enabled() is True
    assert config.get_sound() == "Frog"


def test_completion_message_names_supported_terminal(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "WarpTerminal")

    assert notifier._completion_message() == "Response complete in Warp."


def test_completion_message_is_generic_for_unknown_terminal(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "mystery-terminal")

    assert notifier._completion_message() == "Response complete."


def test_notify_completion_routes_by_platform(monkeypatch):
    calls: list[tuple[str, str, str]] = []
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.setattr(notifier.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        notifier,
        "_notify_macos",
        lambda message, sound: calls.append(("mac", message, sound)),
    )
    notifier.notify_completion("Frog")

    monkeypatch.setattr(notifier.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        notifier,
        "_notify_linux",
        lambda message, sound: calls.append(("linux", message, sound)),
    )
    notifier.notify_completion("")

    monkeypatch.setattr(notifier.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        notifier,
        "_notify_windows",
        lambda message, sound: calls.append(("windows", message, sound)),
    )
    notifier.notify_completion("C:/sound.wav")

    assert calls == [
        ("mac", "Response complete.", "Frog"),
        ("linux", "Response complete.", ""),
        ("windows", "Response complete.", "C:/sound.wav"),
    ]


def test_macos_named_sound_uses_notification_sound(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(notifier, "_run", lambda command: commands.append(command))

    notifier._notify_macos("Response complete.", "Frog")

    assert commands == [
        [
            "/usr/bin/osascript",
            "-e",
            'display notification "Response complete." with title "Code Puppy" sound name "Frog"',
        ]
    ]


def test_explicit_sound_path_must_be_an_existing_absolute_file(tmp_path):
    sound_file = tmp_path / "done.wav"
    sound_file.write_bytes(b"sound")

    assert notifier._sound_file(str(sound_file)) == sound_file
    assert notifier._sound_file("relative.wav") is None
    assert notifier._sound_file(str(tmp_path / "missing.wav")) is None
    assert notifier._sound_file("Frog") is None


def test_linux_without_notify_send_is_still_safe(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(notifier.shutil, "which", lambda _name: None)
    monkeypatch.setattr(notifier, "_run", lambda command: commands.append(command))

    notifier._notify_linux("Response complete.", "")

    assert commands == []


def test_linux_notify_send_uses_argument_vector(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(
        notifier.shutil,
        "which",
        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None,
    )
    monkeypatch.setattr(notifier, "_run", lambda command: commands.append(command))

    notifier._notify_linux("Response complete.", "")

    assert commands == [
        [
            "/usr/bin/notify-send",
            "--app-name",
            "Code Puppy",
            "Code Puppy",
            "Response complete.",
        ]
    ]


def test_windows_sound_path_escapes_apostrophes_without_shell(monkeypatch, tmp_path):
    sound_file = tmp_path / "pup's-done.wav"
    sound_file.write_bytes(b"sound")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        notifier.shutil,
        "which",
        lambda name: "powershell.exe" if name == "powershell" else None,
    )
    monkeypatch.setattr(notifier, "_run", lambda command: commands.append(command))

    notifier._play_file(
        str(sound_file),
        players=(("powershell", "-NoProfile", "-NonInteractive", "-Command"),),
    )

    escaped_path = str(sound_file).replace("'", "''")
    assert commands == [
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(New-Object System.Media.SoundPlayer '{escaped_path}').PlaySync()",
        ]
    ]


def test_callback_starts_one_daemon_worker_for_successful_top_level_response(
    monkeypatch,
):
    thread = Mock()
    thread.start = Mock()
    constructor = Mock(return_value=thread)
    monkeypatch.setattr(callbacks, "is_enabled", lambda: True)
    monkeypatch.setattr(callbacks, "_is_subagent", lambda: False)
    monkeypatch.setattr(callbacks, "get_sound", lambda: "Frog")
    monkeypatch.setattr(callbacks.threading, "Thread", constructor)

    callbacks._on_agent_run_end("agent", "model", "session", True, None, "Finished", {})

    constructor.assert_called_once_with(
        target=callbacks.notify_completion,
        args=("Frog",),
        daemon=True,
        name="code-puppy-completion-notification",
    )
    thread.start.assert_called_once()


def test_callback_ignores_disabled_failed_empty_and_subagent_runs(monkeypatch):
    constructor = Mock()
    monkeypatch.setattr(callbacks.threading, "Thread", constructor)
    monkeypatch.setattr(callbacks, "is_enabled", lambda: False)
    callbacks._on_agent_run_end(success=True, response_text="Done")

    monkeypatch.setattr(callbacks, "is_enabled", lambda: True)
    callbacks._on_agent_run_end(success=False, response_text="Done")
    callbacks._on_agent_run_end(success=True, response_text="")

    monkeypatch.setattr(callbacks, "_is_subagent", lambda: True)
    callbacks._on_agent_run_end(success=True, response_text="Done")

    constructor.assert_not_called()
