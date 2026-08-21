"""Register the Logfire session-mirroring plugin.

Opt-in by design: mirroring ships full chat histories to the user's own
Logfire project, so it stays off until ``enable_logfire_sessions`` is set
(via ``/set`` or ``/logfire-sessions on``) AND a project write token exists
(from ``/logfire onboard``).

``post_autosave`` is a decorative hook surface: a misbehaving mirror must
never poison the save path, so every failure is swallowed here.
"""

from __future__ import annotations

import traceback
from typing import Any

from code_puppy.callbacks import register_callback
from code_puppy.messaging import emit_error, emit_info, emit_success, emit_warning

COMMAND_NAMES = {"logfire-sessions"}

_PULL_STUB = (
    "Restoring sessions from Logfire is not implemented yet. It needs a "
    "read token (the hosted MCP query path scrubs values containing words "
    "like 'session', which would corrupt restored payloads). Prerequisite: "
    "extend ONBOARD_SCOPES with read-token minting permission, then pull via "
    "POST /v2/query and rebuild the envelope locally."
)


def _enabled() -> bool:
    from code_puppy.config import get_value

    return str(get_value("enable_logfire_sessions") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _on_post_autosave(metadata: Any) -> None:
    """Mirror the just-saved session into Logfire. Never raises."""
    try:
        if not _enabled():
            return
        from .mirror import mirror_session

        emitted, total = mirror_session(metadata)
        if emitted:
            emit_info(
                f"Mirrored {emitted} new message(s) of '{metadata.session_name}' "
                f"to Logfire ({total} total)."
            )
    except Exception:
        traceback.print_exc()  # forensics only; the save path must not care


def _status() -> None:
    from ..logfire_oauth.oauth import load_project_credentials

    if not _enabled():
        emit_warning("Session mirroring is disabled (/logfire-sessions on).")
    else:
        emit_info("Session mirroring: enabled")
    credentials = load_project_credentials()
    if credentials is None:
        emit_warning("No Logfire write token (/logfire onboard first).")
    else:
        emit_info(f"Write-token project: {credentials.project_name}")
    from . import sync

    state = sync.load_state()
    sessions = state.get("sessions", {})
    emit_info(f"Locally synced sessions: {len(sessions)}")


def _list(limit: int) -> None:
    from .query import SessionsQueryError, list_sessions

    try:
        rows = list_sessions(limit=limit)
    except SessionsQueryError as exc:
        emit_error(str(exc))
        return
    if not rows:
        emit_info("No mirrored sessions found in Logfire.")
        return
    emit_info(f"Mirrored sessions ({len(rows)}):")
    for row in rows:
        project = row.get("project") or "?"
        remote = row.get("remote") or "-"
        name = row.get("name") or "?"
        messages = row.get("messages", "?")
        last_active = str(row.get("last_active", ""))[:19]
        emit_info(f"  {name}  [{project}]  {messages} msgs  {last_active}  {remote}")


def _handle(command: str, name: str) -> bool | None:
    if name not in COMMAND_NAMES:
        return None

    from code_puppy.config import set_value

    parts = command.split()[1:]
    action = parts[0].lower() if parts else "status"
    args = parts[1:]

    if action == "on":
        set_value("enable_logfire_sessions", "true")
        emit_success("Logfire session mirroring enabled.")
    elif action == "off":
        set_value("enable_logfire_sessions", "false")
        emit_success("Logfire session mirroring disabled.")
    elif action == "list":
        limit = int(args[0]) if args and args[0].isdigit() else 25
        _list(min(limit, 100))
    elif action == "pull":
        emit_warning(_PULL_STUB)
    elif action == "status":
        _status()
    else:
        emit_info("Usage: /logfire-sessions [status|on|off|list [limit]|pull]")
    return True


def _help() -> list[tuple[str, str]]:
    return [
        (
            "logfire-sessions",
            "Mirror chat sessions to Logfire; status/on/off/list/pull",
        )
    ]


register_callback("post_autosave", _on_post_autosave)
register_callback("custom_command_help", _help)
register_callback("custom_command", _handle)
