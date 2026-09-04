"""Best-effort, dependency-free desktop completion notifications."""

from __future__ import annotations

import logging
import ntpath
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

from code_puppy.i18n import add_catalog_dir, t

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 3
_CATALOG_DIR = Path(__file__).parent / "locales"
add_catalog_dir(str(_CATALOG_DIR))
_MACOS_SOUND_NAME = re.compile(r"^[A-Za-z0-9 _-]+$")
# Control characters cannot survive intact inside AppleScript/PowerShell/XML
# source and are never meaningful in a single-line notification, so drop them.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_TITLE_PREVIEW_CHARS = 88
_BODY_PREVIEW_CHARS = 180
_TERMINAL_NAMES = {
    "apple_terminal": "Terminal",
    "iterm.app": "iTerm2",
    "warpterminal": "Warp",
}


def _title(prompt: str = "") -> str:
    context = prompt_preview(prompt)
    if context:
        return t("completion_notification.title_with_context", context=context)
    return t("completion_notification.title")


def notify_completion(
    sound: str = "", prompt: str = "", response_text: str = ""
) -> None:
    """Show a contextual platform-native completion notification."""
    try:
        system = platform.system()
        title = _title(prompt)
        message = _completion_message(response_text)
        if system == "Darwin":
            _notify_macos(title, message, sound)
        elif system == "Linux":
            _notify_linux(title, message, sound)
        elif system == "Windows":
            _notify_windows(title, message, sound)
    except Exception:
        logger.debug("completion notification failed", exc_info=True)


def _completion_message(response_text: str = "") -> str:
    terminal = _TERMINAL_NAMES.get(os.environ.get("TERM_PROGRAM", "").casefold())
    preview = response_preview(response_text)
    if preview:
        return (
            preview
            if terminal is None
            else t(
                "completion_notification.response_preview_in_terminal",
                preview=preview,
                terminal=terminal,
            )
        )
    return (
        t("completion_notification.response_complete")
        if terminal is None
        else t(
            "completion_notification.response_complete_in_terminal", terminal=terminal
        )
    )


def response_preview(response_text: str) -> str:
    """Return a safe, bounded body preview for an agent response."""
    return _preview(response_text, _BODY_PREVIEW_CHARS)


def prompt_preview(prompt: str) -> str:
    """Return a safe, bounded title preview for a user prompt."""
    return _preview(prompt, _TITLE_PREVIEW_CHARS)


def _preview(text: str, limit: int) -> str:
    """Collapse dynamic content into a readable, bounded notification line."""
    compact = _WHITESPACE.sub(" ", _CONTROL_CHARS.sub(" ", text or "")).strip()
    if len(compact) <= limit:
        return compact
    shortened = compact[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;:!?")
    return f"{shortened or compact[: limit - 1]}…"


def _notify_macos(title: str, message: str, sound: str) -> None:
    body = _escape_applescript(message)
    title = _escape_applescript(title)
    script = f'display notification "{body}" with title "{title}"'
    if _is_macos_sound_name(sound):
        script += f' sound name "{sound}"'
    _run(["/usr/bin/osascript", "-e", script])
    _play_file(sound, players=(("/usr/bin/afplay",),))


def _notify_linux(title: str, message: str, sound: str) -> None:
    notify_send = shutil.which("notify-send")
    if notify_send:
        # "--" stops option parsing so a title/message starting with "-" is
        # never misread as a flag.
        _run([notify_send, "--app-name", _title(), "--", title, message])
    _play_file(sound, players=(("paplay",), ("aplay",)))


def _notify_windows(title: str, message: str, sound: str) -> None:
    # Windows Runtime toast APIs are available on current Windows releases. This
    # remains best-effort: restricted hosts can reject the call without affecting
    # the completed Code Puppy run.
    toast_xml = (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{_escape_xml(title)}</text><text>{_escape_xml(message)}</text>"
        "</binding></visual></toast>"
    )
    # XML-escaping already removes single quotes, but escape the assembled
    # literal defensively before it enters the single-quoted PowerShell string.
    toast_literal = toast_xml.replace("'", "''")
    notifier_title = _title().replace("'", "''")
    script = (
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
        f"$xml.LoadXml('{toast_literal}'); "
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        f"CreateToastNotifier('{notifier_title}').Show("
        "[Windows.UI.Notifications.ToastNotification]::new($xml))"
    )
    powershell = _powershell_executable()
    _run([powershell, "-NoProfile", "-NonInteractive", "-Command", script])
    _play_file(
        sound,
        players=((powershell, "-NoProfile", "-NonInteractive", "-Command"),),
    )


def _powershell_executable() -> str:
    """Return Windows PowerShell by trusted absolute path, never PATH lookup."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return ntpath.join(
        system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )


def _is_macos_sound_name(sound: str) -> bool:
    return bool(sound) and "/" not in sound and bool(_MACOS_SOUND_NAME.fullmatch(sound))


def _escape_applescript(text: str) -> str:
    """Escape text for a double-quoted AppleScript string literal.

    i18n catalogs are an untrusted plugin/community seam, so translated text is
    treated as hostile input. Escaping backslash and double-quote (after dropping
    control characters) prevents breaking out of the literal into ``do shell
    script`` and neutralizes the injection class entirely.
    """
    text = _CONTROL_CHARS.sub(" ", text)
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _escape_xml(text: str) -> str:
    """Escape text for a Windows toast XML text node (untrusted i18n input)."""
    text = _CONTROL_CHARS.sub(" ", text)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _play_file(sound: str, *, players: tuple[tuple[str, ...], ...]) -> None:
    path = _sound_file(sound)
    if path is None:
        return
    for player in players:
        if Path(player[0]).is_absolute() or ntpath.isabs(player[0]):
            executable = player[0]
        else:
            executable = shutil.which(player[0]) or (
                player[0] if Path(player[0]).is_file() else None
            )
        if executable is None:
            continue
        if ntpath.basename(player[0]).casefold() == "powershell.exe":
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
