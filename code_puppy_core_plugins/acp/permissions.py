"""ACP permission integration — the client's dialog becomes the approval authority.

Code Puppy has two approval edges, and this module wires both to the client via the
SDK's ``AgentSideConnection.request_permission`` — without touching core tool
logic or forcing yolo mode:

* **File operations** go through the pluggable *approval backend* seam in
  ``code_puppy.tools.common``. Sync file tools run in Code Puppy's tool
  threadpool, so the backend bridges to the ACP event loop via
  ``run_coroutine_threadsafe`` to ask the client, then blocks the worker thread for
  the answer. The loop stays free to service the round-trip — no deadlock.
* **Shell commands** go through core's shared shell gate
  (``get_user_approval_async`` -> the same approval backend), so yolo mode and
  sub-agent handling stay in core. Cores older than ``can_prompt_for_approval``
  lack that gate, so a ``run_shell_command`` fallback hook is registered for them
  only.

Neither edge has a timeout: a dialog waits for a person. Both fail **closed**
(deny) if the connection is gone, the client errors, or the session is cancelled
or closed (``cancel_pending``). A denial nobody chose is reported as
``PermissionNotDecided`` rather than as the user rejecting the operation, so the
model is not told the user said no.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import uuid
from typing import Any, Dict, Optional, Set, Tuple

from acp.schema import PermissionOption, ToolCallUpdate

from code_puppy.callbacks import register_callback
from code_puppy_core_plugins.acp import state

try:
    from code_puppy.tools.file_permission_state import PermissionNotDecided
except ImportError:  # pragma: no cover - code_puppy without the marker type

    class PermissionNotDecided(str):  # type: ignore[no-redef]
        """Stand-in for older cores, which report every denial as a rejection."""


logger = logging.getLogger(__name__)

# In-flight file-approval bridges per session, so ``cancel_pending`` can unblock
# the tool threads waiting on them.
_PENDING: Dict[str, Set[concurrent.futures.Future]] = {}
_PENDING_LOCK = threading.Lock()

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
    selected Reject. Every other denial -- no connection, a failed request, a
    cancelled dialog, an unknown option -- returns
    ``(False, PermissionNotDecided(reason))``.
    """
    connection = state.get_connection()
    if connection is None:
        return False, PermissionNotDecided("The client connection is unavailable.")
    try:
        response = await connection.request_permission(
            options=list(_OPTIONS),
            session_id=session_id,
            tool_call=_tool_call_ref(title),
        )
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


def _track_pending(session_id: str, future: concurrent.futures.Future) -> None:
    with _PENDING_LOCK:
        _PENDING.setdefault(session_id, set()).add(future)


def _untrack_pending(session_id: str, future: concurrent.futures.Future) -> None:
    with _PENDING_LOCK:
        futures = _PENDING.get(session_id)
        if futures is None:
            return
        futures.discard(future)
        if not futures:
            del _PENDING[session_id]


def cancel_pending(session_id: Optional[str] = None) -> None:
    """Cancel in-flight file approvals for one session, or all when ``None``.

    Each cancelled wait returns ``PermissionNotDecided``, so the blocked tool
    thread unwinds instead of waiting on a dialog nobody will answer.
    """
    with _PENDING_LOCK:
        if session_id is None:
            futures = [f for group in _PENDING.values() for f in group]
        else:
            futures = list(_PENDING.get(session_id, ()))
    # Cancel outside the lock: cancelling runs callbacks that may re-enter tracking.
    for future in futures:
        future.cancel()


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
    _track_pending(session_id, future)
    try:
        allowed, not_decided = future.result()
    except concurrent.futures.CancelledError:
        return False, PermissionNotDecided("The permission request was cancelled.")
    except Exception:  # noqa: BLE001
        # Abandon the in-flight request so it doesn't linger on the loop.
        future.cancel()
        logger.exception("ACP approval bridge failed; denying")
        return False, PermissionNotDecided(
            "The permission request could not be completed."
        )
    finally:
        _untrack_pending(session_id, future)
    return bool(allowed), not_decided


async def _legacy_shell_hook(
    context: Any, command: str, cwd: Optional[str] = None, timeout: int = 60
) -> Optional[Dict[str, Any]]:
    """``run_shell_command`` fallback for cores without core's shell approval gate.

    Mirrors that gate's yolo and sub-agent skips. Returns ``None`` to allow, or a
    ``{"blocked": True, ...}`` dict to deny. Inert outside ACP mode.
    """
    if state.get_active_session_id() is None:
        return None
    from code_puppy.config import get_yolo_mode
    from code_puppy.tools.subagent_context import is_subagent

    if get_yolo_mode() or is_subagent():
        return None
    allowed, not_decided = await asyncio.to_thread(
        _approval_backend, "Shell Command", f"Run shell command: {command}", None
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


_legacy_shell_hook_registered = False


def install() -> None:
    """Install the file approval backend, plus the legacy shell hook on old cores."""
    from code_puppy.tools import common

    global _legacy_shell_hook_registered
    common.set_approval_backend(_approval_backend)
    # Remove once the minimum supported core ships can_prompt_for_approval: until
    # then an older core would run shell commands without asking the client.
    if not hasattr(common, "can_prompt_for_approval"):
        register_callback("run_shell_command", _legacy_shell_hook)
        _legacy_shell_hook_registered = True


def uninstall() -> None:
    """Remove the approval backend and legacy hook so normal stdin prompting resumes."""
    from code_puppy.callbacks import unregister_callback
    from code_puppy.tools import common

    global _legacy_shell_hook_registered
    common.set_approval_backend(None)
    if _legacy_shell_hook_registered:
        _legacy_shell_hook_registered = False
        try:
            unregister_callback("run_shell_command", _legacy_shell_hook)
        except Exception:  # noqa: BLE001
            logger.debug("ACP: run_shell_command unregister failed", exc_info=True)
