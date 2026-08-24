"""Register the session-namer plugin.

Two triggers, both decorative (failures never surface):

* ``post_autosave`` -- name the just-saved session when it has never
  been AI-named or has grown by ``RENAME_DELTA`` messages since. The
  previous summary is fed back in, so titles refine instead of thrash.
* ``session_browser_open`` -- when ``/resume`` opens, backfill up to
  ``BACKFILL_LIMIT`` of the newest un-named sessions. The browser hands
  over its live metadata dicts, so names pop in on repaint as the
  worker finishes them.

Enabled by default; ``/set session_namer off`` disables, and
``session_namer_model`` overrides the model (defaults to the current
agent/global model via the ``btw`` resolution chain).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from code_puppy.callbacks import register_callback

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    try:
        from code_puppy.config import get_value

        raw = get_value("session_namer")
        if raw is None or str(raw).strip() == "":
            return True  # on by default -- the naming IS the feature
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}
    except Exception:
        return False


def _on_post_autosave(metadata: Any) -> None:
    """Queue naming for a freshly autosaved session. Never raises."""
    try:
        if not _enabled():
            return
        from code_puppy.command_line.session_browser_data import (
            _get_session_metadata,
        )

        from .namer import naming_needed, submit

        # Sidecar is the source of truth for ai_named_at; re-read it.
        base_dir = Path(metadata.json_path).parent
        meta = _get_session_metadata(base_dir, metadata.session_name)
        meta.setdefault("message_count", metadata.message_count)
        if naming_needed(meta):
            submit(base_dir, metadata.session_name)
    except Exception:
        logger.debug("session_namer: post_autosave hook failed", exc_info=True)


def _on_session_browser_open(base_dir: str, entries: list) -> None:
    """Backfill names for the newest un-named sessions. Never raises."""
    try:
        if not _enabled():
            return
        from .namer import BACKFILL_LIMIT, naming_needed, submit

        queued = 0
        directory = Path(base_dir)
        for session_name, meta in entries:
            if queued >= BACKFILL_LIMIT:
                break
            if isinstance(meta, dict) and naming_needed(meta):
                if submit(directory, session_name, live_meta=meta):
                    queued += 1
    except Exception:
        logger.debug("session_namer: browser_open hook failed", exc_info=True)


register_callback("post_autosave", _on_post_autosave)
try:
    register_callback("session_browser_open", _on_session_browser_open)
except ValueError:
    # Core predates the session_browser_open hook; autosave naming still works.
    logger.debug("session_namer: core lacks session_browser_open hook")
