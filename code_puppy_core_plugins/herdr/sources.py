"""Fail-soft adapters from code-puppy internals to herdr report payloads.

This is the single seam between the herdr plugin and the rest of
code-puppy. Every function here catches ordinary runtime failures, logs at
debug level, and returns a safe fallback -- reporting to herdr must never
be able to disturb the agent. Reporter/client code depends only on this
module's return shapes, and its tests mock this module.

Adapters:

* :func:`current_tokens_payload` -- a static-keyed, string-valued map of
  ``model`` / ``context`` / ``tokens`` for ``pane.report_metadata``.
* :func:`current_session_title` -- the session namer's auto-generated
  conversation title (the ``/resume`` name) for the pane presentation
  title, read from the session metadata sidecar.
* :func:`naming_enabled` -- whether the ``session_namer`` plugin is
  enabled, so the reporter only waits for a title a naming job will
  produce.
* :func:`current_session_name` / :func:`current_session_ref` -- a stable
  ``(session_id, session_path)`` reference for
  ``pane.report_agent_session``.
* :func:`activity_message` -- a short human-readable activity string for
  the decorative ``message`` field on ``pane.report_agent``.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: herdr caps metadata values; keep every value comfortably short.
_MAX_VALUE_LEN = 128

#: herdr clips the pane presentation title at 80 characters.
_TITLE_MAX_LEN = 80


def _compact_tokens(n: int) -> str:
    """Render a token count compactly: 999 -> ``999``; 48200 -> ``48k``."""
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n // 1000}k"
    return f"{n // 1_000_000}M"


def _clip(value: object) -> str:
    """Coerce to a string and cap length so a payload can't balloon."""
    text = str(value)
    return text[:_MAX_VALUE_LEN]


def _current_model() -> Optional[str]:
    try:
        from code_puppy.agents.agent_manager import get_current_agent

        agent = get_current_agent()
        if agent is None:
            return None
        return agent.get_model_name()
    except Exception:
        logger.debug("herdr: could not resolve current model", exc_info=True)
        return None


def current_tokens_payload() -> Optional[Dict[str, str]]:
    """Return a static-keyed metadata map, or ``None`` when unavailable.

    Keys are always ``model``, ``context``, and ``tokens``; values are
    coerced to strings and clipped. Indicator glyphs are omitted on purpose
    -- herdr rows already carry their own state icons. Returns ``None`` if
    context usage can't be computed so the pane keeps its last good values
    (which the metadata TTL eventually clears).
    """
    try:
        from code_puppy.token_usage import get_current_usage

        usage = get_current_usage()
        if usage is None:
            return None
        payload: Dict[str, str] = {
            "context": _clip(f"{round(usage.percent)}%"),
            "tokens": _clip(
                f"{_compact_tokens(usage.total_tokens)}/{_compact_tokens(usage.capacity)}"
            ),
        }
        model = _current_model()
        if model:
            payload["model"] = _clip(model)
        return payload
    except Exception:
        logger.debug("herdr: token payload unavailable", exc_info=True)
        return None


def current_session_name() -> Optional[str]:
    """The durable current session name, or ``None``. Never raises."""
    try:
        from code_puppy.config import get_current_session_name

        return get_current_session_name() or None
    except Exception:
        logger.debug("herdr: session name unavailable", exc_info=True)
        return None


def _session_sidecar_meta(session_name: str) -> dict:
    """The session's metadata sidecar as a dict. ``{}`` when unreadable."""
    try:
        from pathlib import Path

        from code_puppy.config import AUTOSAVE_DIR
        from code_puppy.session_storage import build_session_paths

        paths = build_session_paths(Path(AUTOSAVE_DIR), session_name)
        with paths.metadata_path.open(encoding="utf-8") as file:
            meta = json.load(file)
        return meta if isinstance(meta, dict) else {}
    except Exception:
        logger.debug("herdr: sidecar unreadable", exc_info=True)
        return {}


def current_session_title() -> Optional[str]:
    """The current session's auto-named title (the ``/resume`` name).

    Read from the metadata sidecar the ``session_namer`` plugin maintains.
    ``None`` when the session is unnamed or unreadable -- fail-soft, never
    raises. Clipped to herdr's presentation-title limit.
    """
    name = current_session_name()
    if name is None:
        return None
    title = _session_sidecar_meta(name).get("title")
    if not isinstance(title, str):
        return None
    title = title.strip()
    if not title:
        return None
    return title[:_TITLE_MAX_LEN]


def naming_enabled() -> bool:
    """Whether the ``session_namer`` plugin is enabled for this process.

    Mirrors the namer's own gate (the ``session_namer`` config value, on by
    default) so the herdr plugin only waits for a title a naming job will
    produce. The namer keeps its disable list private, so this mirrors the
    check instead of importing across plugins.
    """
    try:
        from code_puppy.config import get_value

        raw = get_value("session_namer")
        if raw is None or str(raw).strip() == "":
            return True  # on by default -- the naming IS the feature
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}
    except Exception:
        return False


def current_session_ref() -> Optional[Tuple[str, str]]:
    """Return ``(session_id, session_path)`` for the process's autosave.

    The name is stable for the life of a session (until ``/clear``,
    ``/session new``, resume, load, or rotation), and the pickle path stays
    canonical because the CLI pins the resolved stem and writes later saves
    to ``AUTOSAVE_DIR``. Returns ``None`` on any failure.
    """
    name = current_session_name()
    if not name:
        return None
    try:
        from pathlib import Path

        from code_puppy.config import AUTOSAVE_DIR
        from code_puppy.session_storage import build_session_paths

        paths = build_session_paths(Path(AUTOSAVE_DIR), name)
        return name, str(paths.pickle_path)
    except Exception:
        logger.debug("herdr: session ref unavailable", exc_info=True)
        return None


def activity_message(tool_name: str) -> str:
    """Return a short activity string for a starting tool call.

    ``read_file`` -> ``running read file``. Fail-soft: an odd or missing
    name degrades to a generic ``running tool``.
    """
    try:
        humanized = str(tool_name).replace("_", " ").strip()
        if not humanized:
            return "running tool"
        return f"running {humanized}"
    except Exception:
        logger.debug("herdr: could not humanize tool name", exc_info=True)
        return "running tool"


__all__ = [
    "current_tokens_payload",
    "current_session_name",
    "current_session_title",
    "current_session_ref",
    "naming_enabled",
    "activity_message",
]
