"""Resume the previous code-puppy session when re-entering a herdr pane.

herdr can resume *official* agent panes after a server restart by reading the
native session reference an integration reported -- but code-puppy is not an
official herdr integration today, so herdr stores nothing for us. (Verified
against herdr 0.9.1: ``pane.report_agent_session`` is accepted, but
``pane.get`` exposes no ``agent_session`` for the ``codepuppy`` agent, while
the same call for ``codex`` does.) This module makes code-puppy
self-sufficient:

* it keeps a tiny local **pane -> session map** under ``XDG_STATE_HOME``,
  written whenever we report a session reference, so a restored pane can find
  the conversation it was last running;
* on startup inside a herdr pane it resolves the previous session -- first
  from herdr's own stored reference (forward-compatible with a future herdr
  that recognises codepuppy), then from the local map -- and loads it into the
  current agent, exactly like ``-r`` does.

State deliberately lives under ``XDG_STATE_HOME`` and never inside the plugin
directory: runtime state next to plugin code would self-tamper the plugin's
trust hash and demand re-acceptance on every boot.

Everything here is fail-soft. Restoring a session is a convenience; a broken
socket, a missing file, or an unreadable store must never block startup.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable, Optional, Tuple

from .client import HerdrClient

try:  # pragma: no cover - POSIX only
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

logger = logging.getLogger(__name__)

_STATE_FILENAME = "herdr_pane_sessions.json"

#: ``(session_name, session_dir)`` -- the durable autosave name plus the
#: directory its ``.json``/``.pkl`` file lives in.
SessionRef = Tuple[str, str]


def _state_path() -> Optional[Path]:
    """The pane->session store path, or ``None`` when the dir is unavailable."""
    try:
        from code_puppy.config import STATE_DIR

        return Path(STATE_DIR) / _STATE_FILENAME
    except Exception:
        logger.debug("herdr: state dir unavailable", exc_info=True)
        return None


def _read_store() -> dict:
    """Read the pane->session map. ``{}`` on any failure (never raises)."""
    path = _state_path()
    if path is None:
        return {}
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("herdr: pane-session store unreadable", exc_info=True)
        return {}


def _write_store(store: dict) -> None:
    """Atomically write the map through a writer-private temp file.

    Never raises: fail-soft is this module's contract, and both callers treat a
    failed write as "we simply do not auto-resume next boot".

    The temp name is scoped by PID on purpose. A shared ``.tmp`` name lets two
    code-puppy processes interleave writes into the same file before either
    renames it, which silently drops *other* panes' entries -- and herdr's
    whole point is running many panes at once.
    """
    path = _state_path()
    if path is None:
        return
    tmp: Optional[Path] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as file:
            json.dump(store, file)
        tmp.replace(path)
    except Exception:
        logger.debug("herdr: could not write pane-session store", exc_info=True)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                logger.debug("herdr: could not clean temp store", exc_info=True)


def _locked_update(mutate: Callable[[dict], None]) -> None:
    """Run a read-modify-write of the store under an exclusive lock.

    The store is shared by every code-puppy process on the box, so concurrent
    writers are the *normal* case here. Without the lock a stale reader
    clobbers a newer writer's entry, and that pane silently loses its
    auto-resume -- the exact feature this module exists to provide. The lock
    lives in a sibling ``.lock`` file so ``replace()`` never swaps it out from
    under us. ``fcntl`` is POSIX-only; elsewhere we degrade to best effort
    rather than crash. Never raises.
    """
    path = _state_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_name(path.name + ".lock").open("a+") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                store = _read_store()
                mutate(store)
                _write_store(store)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
    except Exception:
        logger.debug("herdr: could not update pane-session map", exc_info=True)


def remember_session(
    pane_id: Optional[str], session_name: str, session_path: str
) -> None:
    """Record ``pane_id -> (session_name, session_path)``.

    Called from the reporter every time it reports a session reference, so the
    map always reflects the pane's *current* session (a new session after
    ``/clear`` or ``/resume`` overwrites the stale entry). Any failure is
    swallowed -- a missing map only costs us one auto-resume.
    """
    if not pane_id or not session_name:
        return

    def _mutate(store: dict) -> None:
        store[pane_id] = {
            "session_name": session_name,
            "session_path": str(session_path or ""),
        }

    _locked_update(_mutate)


def forget_session(pane_id: Optional[str]) -> None:
    """Drop ``pane_id``'s entry (used when its session is gone). Fail-soft."""
    if not pane_id:
        return

    def _mutate(store: dict) -> None:
        store.pop(pane_id, None)

    _locked_update(_mutate)


def _from_herdr(client: HerdrClient) -> Optional[SessionRef]:
    """herdr's own stored reference, if it has one for this pane.

    Forward-compatible: on a herdr that recognises codepuppy (or grows a
    custom-integration hook) this is the authoritative source. ``kind`` tells
    us whether ``value`` is a bare session id or a file path.
    """
    info = client.get_agent_session()
    if not info:
        return None
    value = info.get("value")
    if not isinstance(value, str) or not value:
        return None
    kind = info.get("kind")
    if kind == "path":
        path = Path(value)
        return path.stem, str(path.parent)
    # ``kind == "id"`` (or an unknown kind): a bare session name resolved
    # against the standard autosave directory.
    return value, ""


def _from_store(pane_id: Optional[str]) -> Optional[SessionRef]:
    """Our local pane->session map entry, if any."""
    if not pane_id:
        return None
    entry = _read_store().get(pane_id)
    if not isinstance(entry, dict):
        return None
    name = entry.get("session_name")
    if not isinstance(name, str) or not name:
        return None
    stored_path = entry.get("session_path")
    parent = ""
    if isinstance(stored_path, str) and stored_path:
        parent = str(Path(stored_path).parent)
    return name, parent


def resolve_stored_session(client: HerdrClient) -> Optional[SessionRef]:
    """Resolve the session this pane last ran: herdr ref first, local map second."""
    return _from_herdr(client) or _from_store(client.pane_id)


def resume_session(client: HerdrClient) -> bool:
    """Load this pane's previous session into the current agent.

    Returns ``True`` only when history was actually restored. Any failure --
    no stored reference, a vanished file, an unreadable store -- returns
    ``False`` so startup simply proceeds as a fresh session. Never raises.
    """
    if not client.active:
        return False
    ref = resolve_stored_session(client)
    if ref is None:
        return False
    session_name, session_dir = ref

    try:
        from code_puppy.agents.agent_manager import get_current_agent
        from code_puppy.config import AUTOSAVE_DIR, pin_current_session_name
        from code_puppy.session_storage import load_session

        base_dir = Path(session_dir) if session_dir else Path(AUTOSAVE_DIR)
        history = load_session(session_name, base_dir)
    except FileNotFoundError:
        logger.debug("herdr: stored session %s is gone", session_name)
        forget_session(client.pane_id)
        return False
    except Exception:
        logger.debug("herdr: could not load stored session", exc_info=True)
        return False

    if not history:
        # An empty lazy-created session is not worth "resuming".
        return False

    try:
        agent = get_current_agent()
        agent.set_message_history(list(history))
        pin_current_session_name(session_name)
    except Exception:
        logger.debug("herdr: could not adopt stored session", exc_info=True)
        return False

    _announce(session_name, history)
    return True


def _announce(session_name: str, history: list) -> None:
    """Tell the user their pane's session came back. Best-effort."""
    try:
        from code_puppy.messaging import emit_success

        emit_success(
            f"herdr: resumed session '{session_name}' ({len(history)} messages)"
        )
    except Exception:
        logger.debug("herdr: could not announce resume", exc_info=True)
    try:
        from code_puppy.command_line.autosave_menu import display_resumed_history

        display_resumed_history(history)
    except Exception:
        logger.debug("herdr: could not display resumed history", exc_info=True)


__all__ = [
    "SessionRef",
    "remember_session",
    "forget_session",
    "resolve_stored_session",
    "resume_session",
]
