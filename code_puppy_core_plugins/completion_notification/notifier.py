"""Best-effort, dependency-free desktop completion notifications."""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_TITLE = "Code Puppy"
_TIMEOUT_SECONDS = 3
_MACOS_SOUND_NAME = re.compile(r"^[A-Za-z0-9 _-]+$")
_TERMINAL_NAMES = {
    "apple_terminal": "Terminal",
    "iterm.app": "iTerm2",
    "warpterminal": "Warp",
}


def notify_completion(sound: str = "") -> None:
    """Show a platform-native completion notification without raising."""
    try:
        system = platform.system()
        message = _completion_message()
        if system == "Darwin":
            _notify_macos(message, sound)
        elif system == "Linux":
            _notify_linux(message, sound)
        elif system == "Windows":
            _notify_windows(message, sound)
    except Exception:
        logger.debug("completion notification failed", exc_info=True)


def _completion_message() -> str:
    terminal = _TERMINAL_NAMES.get(os.environ.get("TERM_PROGRAM", "").casefold())
    return (
        "Response complete."
        if terminal is None
        else f"Response complete in {terminal}."
    )


def _notify_macos(message: str, sound: str) -> None:
    script = f'display notification "{message}" with title "{_TITLE}"'
    if _is_macos_sound_name(sound):
        script += f' sound name "{sound}"'
    _run(["/usr/bin/osascript", "-e", script])
    _play_file(sound, players=(("/usr/bin/afplay",),))


def _notify_linux(message: str, sound: str) -> None:
    notify_send = shutil.which("notify-send")
    if notify_send:
        _run([notify_send, "--app-name", _TITLE, _TITLE, message])
    _play_file(sound, players=(("paplay",), ("aplay",)))


def _notify_windows(message: str, sound: str) -> None:
    # Windows Runtime toast APIs are available on current Windows releases. This
    # remains best-effort: restricted hosts can reject the call without affecting
    # the completed Code Puppy run.
    escaped_message = message.replace("'", "''")
    toast_xml = (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{_TITLE}</text><text>{escaped_message}</text>"
        "</binding></visual></toast>"
    )
    script = (
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
        f"$xml.LoadXml('{toast_xml}'); "
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        f"CreateToastNotifier('{_TITLE}').Show("
        "[Windows.UI.Notifications.ToastNotification]::new($xml))"
    )
    _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    _play_file(
        sound, players=(("powershell", "-NoProfile", "-NonInteractive", "-Command"),)
    )


def _is_macos_sound_name(sound: str) -> bool:
    return bool(sound) and "/" not in sound and bool(_MACOS_SOUND_NAME.fullmatch(sound))


def _play_file(sound: str, *, players: tuple[tuple[str, ...], ...]) -> None:
    path = _sound_file(sound)
    if path is None:
        return
    for player in players:
        executable = shutil.which(player[0]) or (
            player[0] if Path(player[0]).is_file() else None
        )
        if executable is None:
            continue
        if player[0] == "powershell":
            escaped_path = str(path).replace("'", "''")
            _run(
                [
                    *player,
                    f"(New-Object System.Media.SoundPlayer '{escaped_path}').PlaySync()",
                ]
            )
        else:
            _run([executable, *player[1:], str(path)])
        return


def _sound_file(sound: str) -> Path | None:
    if not sound or _is_macos_sound_name(sound):
        return None
    path = Path(sound).expanduser()
    if not path.is_absolute() or not path.is_file():
        return None
    return path


def _run(command: list[str]) -> None:
    try:
        subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug(
            "completion notification command failed: %s", command[0], exc_info=True
        )
