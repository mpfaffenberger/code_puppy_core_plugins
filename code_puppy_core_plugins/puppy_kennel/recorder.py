"""Recorder — captures agent runs into the kennel.

Fires on ``agent_run_end``. The recorder writes the agent's response to
**one** wing: the current repo wing. The autosave path treats every
response as "a conversation that happened in this project" — that's the
honest framing.

Two different ids, two different homes:

* **Room name** — stable conversation fingerprint. Same shape as the
  legacy label (``session-<8 hex>``), but derived from
  ``get_current_session_name()`` so multi-turn dialogue in one window
  (and ``-r`` resume of that session) shares one room. Separate windows
  / ``/clear`` get distinct rooms.
* **``drawers.session_id``** — the per-run ``group_id`` UUID from the
  runtime hook (unchanged column semantics).

Cross-project agent reflections belong in ``agent:<name>`` and are an
opt-in concern handled by the ``kennel_remember`` tool, not autosave.
User preferences likewise live in ``user:default`` and only land there
via explicit ``kennel_remember`` calls.

Capturing user input is a phase-2 concern that will hook ``stream_event``
instead.
"""

from __future__ import annotations

from typing import Any

from code_puppy.messaging.bus import emit_debug

from . import kennel
from .state import is_enabled
from .wings import conversation_session_name, detect_cwd, repo_wing, room_name


def record_run_end(
    agent_name: str,
    model_name: str,
    session_id: str | None = None,
    success: bool = True,
    error: Exception | None = None,
    response_text: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Persist the agent's final response into the kennel.

    Writes a single drawer to the repo wing (shared project memory).
    Room membership uses the stable conversation session name (short
    ``session-<8 hex>`` label). ``drawers.session_id`` stores the
    per-run group_id UUID from the hook. Failures here must never crash
    the host app — the kennel is best-effort memory, not a transactional
    system of record.
    """
    if not response_text or not response_text.strip():
        return
    if not success:
        # Don't memorialize broken runs. The error log is the right place.
        return
    if not is_enabled():
        # Memory is toggled off — silently skip. Slash commands surface state.
        return

    try:
        cwd = detect_cwd()
        conversation_id = conversation_session_name()
        room = room_name(conversation_id)
        drawer_meta: dict[str, Any] = {
            "agent": agent_name,
            "model": model_name,
            "cwd": str(cwd),
            "conversation_id": conversation_id,
        }
        if metadata:
            drawer_meta["run_metadata"] = metadata

        # Shared project memory — the only autosave destination.
        repo_w = repo_wing(cwd)
        repo_wing_id = kennel.ensure_wing(repo_w)
        repo_room_id = kennel.ensure_room(repo_wing_id, room)
        kennel.add_drawer(
            repo_room_id,
            content=response_text,
            role="assistant",
            # Column keeps historical semantics: per-run group_id UUID.
            session_id=session_id,
            metadata=drawer_meta,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort memory.
        emit_debug(f"[puppy_kennel] recorder skipped: {exc!r}")
