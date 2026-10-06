"""ACP permission integration — the client's dialog becomes the approval authority.

Code Puppy has two approval edges, and this module wires both to the client via the
SDK's ``AgentSideConnection.request_permission`` — without touching core tool
logic or forcing yolo mode:

* **File operations** go through the pluggable *approval backend* seam in
  ``code_puppy.tools.common``. Sync file tools run in Code Puppy's tool
  threadpool, so the backend bridges to the ACP event loop via
  ``run_coroutine_threadsafe`` to ask the client, then blocks the worker thread for
  the answer. The loop stays free to service the round-trip — no deadlock.
* **Shell commands** go through the ``run_shell_command`` hook, which is async
  and already runs on the ACP loop, so it can ``await`` the client directly.

Both edges fail **closed** (deny) if the connection is gone or the client errors.
A denial nobody chose -- a cancelled, failed or timed-out request, or an option
we do not recognize -- is reported as ``PermissionNotDecided`` rather than as the
user rejecting the operation, so the model is not told the user said no.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, Optional, Tuple

from acp.schema import PermissionOption, ToolCallUpdate

from code_puppy.callbacks import register_callback
from code_puppy_core_plugins.acp import state

try:
    from code_puppy.tools.file_permission_state import PermissionNotDecided
except ImportError:  # pragma: no cover - code_puppy without the marker type

    class PermissionNotDecided(str):  # type: ignore[no-redef]
        """Stand-in for older cores, which report every denial as a rejection."""


logger = logging.getLogger(__name__)

# How long to wait for a human in the client to answer a permission dialog. Dialogs
# can sit for a while; deny (fail closed) if it's truly abandoned.
_PERMISSION_TIMEOUT_S = 600

# The allow/deny options we present. The client echoes back the chosen ``option_id``.
_OPTIONS = [
    PermissionOption(option_id="allow_once", name="Allow", kind="allow_once"),
    PermissionOption(option_id="reject_once", name="Reject", kind="reject_once"),
]
_ALLOW_IDS = {"allow_once"}
_REJECT_IDS = {"reject_once"}


def _tool_call_ref(title: str) -> ToolCallUpdate:
    """Build the ``toolCall`` ref a permission request attaches to.

    Reuses the id/title of the tool call currently open (set by
    ``pre_tool_call``) so the client pins the dialog to the right agent-panel entry;
    falls back to a standalone id when no tool call is active.
    """
    current = state.current_tool_call()
    if current is not None:
        name, tool_call_id = current
        return ToolCallUpdate(tool_call_id=tool_call_id, title=name)
    return ToolCallUpdate(tool_call_id=f"perm_{uuid.uuid4().hex[:12]}", title=title)


async def _ask_client_outcome(
    session_id: str, title: str
) -> Tuple[bool, Optional[PermissionNotDecided]]:
    """Show an allow/deny dialog in the client.

    Returns ``(True, None)`` when allowed and ``(False, None)`` when a person
    selected Reject. Every other denial -- no connection, a failed or
    timed-out request, a cancelled dialog, an unknown option -- returns
    ``(False, PermissionNotDecided(reason))``.
    """
    connection = state.get_connection()
    if connection is None:
        return False, PermissionNotDecided("The client connection is unavailable.")
    try:
        response = await asyncio.wait_for(
            connection.request_permission(
                options=list(_OPTIONS),
                session_id=session_id,
                tool_call=_tool_call_ref(title),
            ),
            timeout=_PERMISSION_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("session/request_permission timed out; denying")
        return False, PermissionNotDecided("The permission request timed out.")
    except Exception:  # noqa: BLE001
        logger.exception("session/request_permission failed; denying")
        return False, PermissionNotDecided(
            "The permission request could not be completed."
        )
    outcome = getattr(response, "outcome", None)
    if getattr(outcome, "outcome", None) == "selected":
        option_id = getattr(outcome, "option_id", None)
        if option_id in _ALLOW_IDS:
            return True, None
        if option_id in _REJECT_IDS:
            return False, None
        return False, PermissionNotDecided(
            "The client returned an unknown permission choice."
        )
    return False, PermissionNotDecided(
        "The permission request was cancelled without a decision."
    )


async def _ask_client(session_id: str, title: str) -> bool:
    """Show an allow/deny dialog in the client; return ``True`` if allowed."""
    allowed, _ = await _ask_client_outcome(session_id, title)
    return allowed


def _approval_backend(
    title: str, message: str, preview: Optional[str]
) -> Tuple[bool, Optional[str]]:
    """Approval backend for file ops; asks the client from the tool threadpool.

    Returns ``(approved, feedback)``. The client's dialog is yes/no, not a
    free-text channel, so feedback is ``None`` for an allow or a Reject, and a
    ``PermissionNotDecided`` when the operation was denied without anyone
    choosing Reject.
    """
    loop = state.get_loop()
    session_id = state.get_active_session_id()
    if loop is None or session_id is None:
        return False, PermissionNotDecided("No client is available to ask.")

    # Guard against being called on the loop thread itself: blocking on
    # run_coroutine_threadsafe there would deadlock (file tools run off-loop).
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        logger.error("Approval backend hit on the ACP loop; denying to avoid deadlock")
        return False, PermissionNotDecided(
            "The permission request could not be completed."
        )

    future = asyncio.run_coroutine_threadsafe(
        _ask_client_outcome(session_id, title), loop
    )
    try:
        # A little longer than the request's own timeout, which answers first.
        allowed, not_decided = future.result(_PERMISSION_TIMEOUT_S + 5)
    except Exception:  # noqa: BLE001 - includes TimeoutError
        # Abandon the in-flight request so it doesn't linger on the loop.
        future.cancel()
        logger.exception("ACP approval bridge failed; denying")
        return False, PermissionNotDecided(
            "The permission request could not be completed."
        )
    return bool(allowed), not_decided


async def _on_run_shell_command(
    context: Any, command: str, cwd: Optional[str] = None, timeout: int = 60
) -> Optional[Dict[str, Any]]:
    """``run_shell_command`` hook: gate shell execution through the client.

    Returns ``None`` to allow (the hook's "no objection"), or a
    ``{"blocked": True, ...}`` dict to deny. Inert outside ACP mode.

    Honors yolo mode: when the user has opted out of approvals we do *not*
    surface a client dialog, matching Code Puppy's file-permission edge (which
    skips its prompt in yolo mode). Otherwise ACP + yolo would silently
    auto-approve file writes yet still prompt for every shell command.
    """
    session_id = state.get_active_session_id()
    if session_id is None:
        return None
    from code_puppy.config import get_yolo_mode

    if get_yolo_mode():
        return None
    allowed, not_decided = await _ask_client_outcome(
        session_id, f"Run shell command: {command}"
    )
    if allowed:
        return None
    if not_decided is not None:
        return {
            "blocked": True,
            "error_message": "Permission not granted",
            "reasoning": (
                f"{not_decided} The command was not run. The user did not "
                "reject it; do not say they did."
            ),
        }
    return {
        "blocked": True,
        "error_message": "Command rejected in the client",
        "reasoning": "The user denied this shell command in the client's permission dialog.",
    }


def install() -> None:
    """Install the file approval backend + the shell permission hook."""
    from code_puppy.tools.common import set_approval_backend

    set_approval_backend(_approval_backend)
    register_callback("run_shell_command", _on_run_shell_command)


def uninstall() -> None:
    """Remove the approval backend + shell hook so normal stdin prompting resumes."""
    from code_puppy.callbacks import unregister_callback
    from code_puppy.tools.common import set_approval_backend

    set_approval_backend(None)
    try:
        unregister_callback("run_shell_command", _on_run_shell_command)
    except Exception:  # noqa: BLE001
        logger.debug("ACP: run_shell_command unregister failed", exc_info=True)
