"""Tests for the opt-in completion-notification plugin."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from code_puppy import i18n

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
        lambda key: " Frog " if key == config.KEY_SOUND else " yes ",
    )

    assert config.is_enabled() is True
    assert config.get_sound() == "Frog"


def test_completion_message_names_supported_terminal(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "WarpTerminal")

    assert notifier._completion_message() == "Response complete in Warp."


def test_completion_message_is_generic_for_unknown_terminal(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "mystery-terminal")

    assert notifier._completion_message() == "Response complete."


def test_completion_message_includes_response_preview(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "WarpTerminal")

    assert notifier._completion_message("Finished safely.") == (
        "Finished safely. · Warp"
    )


def test_notify_completion_routes_by_platform(monkeypatch):
    calls: list[tuple[str, str, str, str]] = []
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.setattr(notifier.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        notifier,
        "_notify_macos",
        lambda title, message, sound: calls.append(("mac", title, message, sound)),
    )
    notifier.notify_completion("Frog", "Which run?", "Finished")

    monkeypatch.setattr(notifier.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        notifier,
        "_notify_linux",
        lambda title, message, sound: calls.append(("linux", title, message, sound)),
    )
    notifier.notify_completion("")

    monkeypatch.setattr(notifier.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        notifier,
        "_notify_windows",
        lambda title, message, sound: calls.append(("windows", title, message, sound)),
    )
    notifier.notify_completion("C:/sound.wav")

    assert calls == [
        ("mac", "Code Puppy — Which run?", "Finished", "Frog"),
        ("linux", "Code Puppy", "Response complete.", ""),
        ("windows", "Code Puppy", "Response complete.", "C:/sound.wav"),
    ]


def test_prompt_and_response_previews_are_single_line_and_bounded():
    prompt = "  Which\nnotification\twas this?  " + " very" * 30
    response = "  Finished\nsafely.  " + " result" * 60

    prompt_preview = notifier.prompt_preview(prompt)
    response_preview = notifier.response_preview(response)

    assert "\n" not in prompt_preview
    assert "\t" not in prompt_preview
    assert len(prompt_preview) <= notifier._TITLE_PREVIEW_CHARS
    assert prompt_preview.endswith("…")
    assert "\n" not in response_preview
    assert len(response_preview) <= notifier._BODY_PREVIEW_CHARS
    assert response_preview.endswith("…")


def test_title_uses_i18n_for_opted_in_prompt(monkeypatch):
    monkeypatch.setattr(notifier, "t", lambda key, **kwargs: f"{key}:{kwargs}")

    assert notifier._title("") == "completion_notification.title:{}"
    assert notifier._title("Which run?") == (
        "completion_notification.title_with_context:{'context': 'Which run?'}"
    )


@pytest.mark.parametrize(
    ("locale", "expected_body"),
    [
        ("en-US", "Response complete."),
        ("es", "Respuesta completada."),
        ("fr-CA", "Réponse terminée."),
    ],
)
def test_contextual_title_preserves_supported_locales(
    monkeypatch, locale, expected_body
):
    previous_locale = i18n.get_locale()
    monkeypatch.setenv("TERM_PROGRAM", "unknown")
    try:
        i18n.set_locale(locale)
        assert notifier._title("Which run?") == "Code Puppy — Which run?"
        assert notifier._completion_message() == expected_body
    finally:
        i18n.set_locale(previous_locale)


def test_contextual_title_supports_pseudolocale(monkeypatch):
    previous_locale = i18n.get_locale()
    monkeypatch.setenv("TERM_PROGRAM", "WarpTerminal")
    try:
        i18n.set_locale("en-XA")
        title = notifier._title("Which run?")
        message = notifier._completion_message("Finished")
        assert title.startswith("⟦")
        assert title.endswith("⟧")
        assert "Which run?" not in title
        assert message.startswith("⟦")
        assert "Finished" not in message
    finally:
        i18n.set_locale(previous_locale)


def test_macos_named_sound_uses_notification_sound(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(notifier, "_run", lambda command: commands.append(command))

    notifier._notify_macos("Code Puppy", "Response complete.", "Frog")

    assert commands == [
        [
            "/usr/bin/osascript",
            "-e",
            'display notification "Response complete." with title "Code Puppy" sound name "Frog"',
        ]
    ]


def test_escape_applescript_neutralizes_injection():
    payload = 'x"\ndo shell script "touch /tmp/PWNED"\ndisplay notification "'
    escaped = notifier._escape_applescript(payload)

    assert "\n" not in escaped  # control characters stripped
    # Every double-quote is escaped, so none can terminate the literal early.
    assert '"' not in escaped.replace('\\"', "")


def test_macos_escapes_untrusted_catalog_text(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(notifier, "_run", lambda command: commands.append(command))
    monkeypatch.setattr(notifier, "_title", lambda _prompt="": 'L\'assistant "Puppy"')

    payload = 'x"\ndo shell script "touch /tmp/PWNED"'
    notifier._notify_macos(notifier._title(), payload, "")

    script = commands[0][2]
    assert "\n" not in script
    assert 'do shell script \\"' in script  # injected quote escaped, not raw
    # Only the four structural AppleScript quotes remain unescaped.
    assert script.replace('\\"', "").count('"') == 4


def test_escape_xml_neutralizes_markup():
    assert notifier._escape_xml("a<b>&\"c'") == "a&lt;b&gt;&amp;&quot;c&apos;"


def test_windows_escapes_untrusted_title_and_message(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setattr(notifier, "_run", lambda command: commands.append(command))
    monkeypatch.setattr(notifier, "_title", lambda _prompt="": "L'assistant")

    notifier._notify_windows(notifier._title(), 'R\u00e9ponse </text>"pwn"', "")

    script = commands[0][4]
    # Title enters a single-quoted PowerShell literal with '' escaping.
    assert "CreateToastNotifier('L''assistant')" in script
    # Injected closing tag is XML-escaped: only the two structural tags remain.
    assert script.count("</text>") == 2
    assert "&lt;/text&gt;" in script


def test_contextual_title_and_response_are_escaped_on_every_platform(monkeypatch):
    prompt = 'Which "run"? </text> L\'assistant\nnext'
    response = 'Done "safely". </text>\nAll checks passed.'
    title = notifier._title(prompt)
    message = notifier._completion_message(response)
    mac_commands: list[list[str]] = []
    windows_commands: list[list[str]] = []
    linux_commands: list[list[str]] = []

    monkeypatch.setattr(notifier, "_run", lambda command: mac_commands.append(command))
    notifier._notify_macos(title, message, "")
    mac_script = mac_commands[0][2]
    assert "\n" not in mac_script
    assert '\\"run\\"' in mac_script
    assert 'Done \\"safely\\"' in mac_script

    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setattr(
        notifier, "_run", lambda command: windows_commands.append(command)
    )
    notifier._notify_windows(title, message, "")
    windows_script = windows_commands[0][4]
    assert "&lt;/text&gt;" in windows_script
    assert "L&apos;assistant" in windows_script

    monkeypatch.setattr(
        notifier.shutil,
        "which",
        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None,
    )
    monkeypatch.setattr(
        notifier, "_run", lambda command: linux_commands.append(command)
    )
    notifier._notify_linux(title, message, "")
    assert linux_commands[0][3] == "--"
    assert linux_commands[0][4].startswith("Code Puppy — Which")


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

    notifier._notify_linux("Code Puppy", "Response complete.", "")

    assert commands == []


def test_linux_notify_send_uses_argument_vector(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(
        notifier.shutil,
        "which",
        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None,
    )
    monkeypatch.setattr(notifier, "_run", lambda command: commands.append(command))

    notifier._notify_linux("Code Puppy", "Response complete.", "")

    assert commands == [
        [
            "/usr/bin/notify-send",
            "--app-name",
            "Code Puppy",
            "--",
            "Code Puppy",
            "Response complete.",
        ]
    ]


def test_windows_sound_path_uses_trusted_absolute_powershell(monkeypatch, tmp_path):
    sound_file = tmp_path / "pup's-done.wav"
    sound_file.write_bytes(b"sound")
    commands: list[list[str]] = []
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setattr(notifier, "_run", lambda command: commands.append(command))

    notifier._notify_windows("Code Puppy", "Response complete.", str(sound_file))

    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    escaped_path = str(sound_file).replace("'", "''")
    assert commands[0][0] == powershell
    assert commands[1] == [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        f"(New-Object System.Media.SoundPlayer '{escaped_path}').PlaySync()",
    ]


def test_completion_message_uses_i18n(monkeypatch):
    monkeypatch.setattr(notifier, "t", lambda key, **kwargs: f"{key}:{kwargs}")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)

    assert (
        notifier._completion_message() == "completion_notification.response_complete:{}"
    )

    monkeypatch.setenv("TERM_PROGRAM", "WarpTerminal")
    assert notifier._completion_message() == (
        "completion_notification.response_complete_in_terminal:{'terminal': 'Warp'}"
    )
    assert notifier._completion_message("Finished") == (
        "completion_notification.response_preview_in_terminal:"
        "{'preview': 'Finished', 'terminal': 'Warp'}"
    )


def test_prompt_is_captured_only_when_notifications_are_enabled(monkeypatch):
    monkeypatch.setattr(callbacks, "_prompts", {})
    monkeypatch.setattr(callbacks, "_is_subagent", lambda: False)
    monkeypatch.setattr(callbacks, "is_enabled", lambda: False)

    callbacks._on_user_prompt_submit("Private prompt", "run-1")
    assert callbacks._prompts == {}

    monkeypatch.setattr(callbacks, "is_enabled", lambda: True)
    callbacks._on_user_prompt_submit("Which\nrun?", "run-1")
    assert callbacks._prompts == {"run-1": "Which run?"}


def test_callback_passes_context_to_worker_and_discards_prompt(monkeypatch):
    worker_calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(callbacks, "_prompts", {"run-1": "Which run?"})
    monkeypatch.setattr(callbacks, "_run_depth", 1)
    monkeypatch.setattr(callbacks, "is_enabled", lambda: True)
    monkeypatch.setattr(callbacks, "_is_subagent", lambda: False)
    monkeypatch.setattr(callbacks, "get_sound", lambda: "Frog")
    monkeypatch.setattr(
        callbacks,
        "_start_notification_worker",
        lambda sound, prompt="", response_text="": worker_calls.append(
            (sound, prompt, response_text)
        ),
    )

    callbacks._on_agent_run_end(
        session_id="run-1", success=True, response_text="Finished"
    )

    assert worker_calls == [("Frog", "Which run?", "Finished")]
    assert callbacks._prompts == {}


def test_callback_starts_one_daemon_worker_for_successful_top_level_response(
    monkeypatch,
):
    thread = Mock()
    thread.start = Mock()
    constructor = Mock(return_value=thread)
    monkeypatch.setattr(callbacks, "_run_depth", 0)
    monkeypatch.setattr(callbacks, "is_enabled", lambda: True)
    monkeypatch.setattr(callbacks, "_is_subagent", lambda: False)
    monkeypatch.setattr(callbacks, "get_sound", lambda: "Frog")
    monkeypatch.setattr(callbacks.threading, "Thread", constructor)

    callbacks._on_agent_run_end("agent", "model", "session", True, None, "Finished", {})

    constructor.assert_called_once()
    assert constructor.call_args.kwargs["daemon"] is True
    assert constructor.call_args.kwargs["name"] == "code-puppy-completion-notification"
    thread.start.assert_called_once()


def test_callback_ignores_disabled_failed_empty_and_subagent_runs(monkeypatch):
    constructor = Mock()
    monkeypatch.setattr(callbacks, "_run_depth", 0)
    monkeypatch.setattr(callbacks.threading, "Thread", constructor)
    monkeypatch.setattr(callbacks, "is_enabled", lambda: False)
    callbacks._on_agent_run_end(success=True, response_text="Done")

    monkeypatch.setattr(callbacks, "is_enabled", lambda: True)
    callbacks._on_agent_run_end(success=False, response_text="Done")
    callbacks._on_agent_run_end(success=True, response_text="")

    monkeypatch.setattr(callbacks, "_is_subagent", lambda: True)
    callbacks._on_agent_run_end(success=True, response_text="Done")

    constructor.assert_not_called()


def test_callback_suppresses_nested_runs_until_the_outer_run_finishes(monkeypatch):
    constructor = Mock()
    monkeypatch.setattr(callbacks, "_run_depth", 0)
    monkeypatch.setattr(callbacks, "is_enabled", lambda: True)
    monkeypatch.setattr(callbacks, "_is_subagent", lambda: False)
    monkeypatch.setattr(callbacks.threading, "Thread", constructor)

    callbacks._on_agent_run_start()
    callbacks._on_agent_run_start()
    callbacks._on_agent_run_end(success=True, response_text="Inner response")

    constructor.assert_not_called()

    callbacks._on_agent_run_end(success=True, response_text="Outer response")

    constructor.assert_called_once()


def test_shutdown_drains_notification_workers(monkeypatch):
    worker = Mock()
    monkeypatch.setattr(callbacks, "_workers", {worker})

    callbacks._drain_workers()

    worker.join.assert_called_once()
    (timeout,) = (worker.join.call_args.kwargs.get("timeout"),)
    assert 0.0 < timeout <= callbacks._WORKER_JOIN_TIMEOUT_SECONDS
    assert callbacks._workers == set()


def test_drain_shares_one_deadline_across_workers(monkeypatch):
    """Total join budget is bounded by the timeout, not timeout * worker count."""
    timeouts: list[float] = []

    def make_worker():
        worker = Mock()
        worker.join = Mock(side_effect=lambda timeout: timeouts.append(timeout))
        return worker

    monkeypatch.setattr(callbacks, "_workers", {make_worker() for _ in range(3)})

    callbacks._drain_workers()

    assert len(timeouts) == 3
    # Each successive join gets the remaining budget, never a fresh full timeout.
    assert all(0.0 <= t <= callbacks._WORKER_JOIN_TIMEOUT_SECONDS for t in timeouts)
