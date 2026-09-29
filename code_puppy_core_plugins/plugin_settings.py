"""Declare a plugin's `/set` settings through core's ``register_settings`` hook.

One place for the old-core fallback: cores that predate the hook reject the
phase with ``ValueError``. The plugin must keep working there; its settings
just don't show up in `/set` autocomplete or the `/set` menu.
"""

from __future__ import annotations

import logging
from typing import Any

from code_puppy.callbacks import register_callback

logger = logging.getLogger(__name__)


def register_settings(settings: Any) -> None:
    """Register a ``SettingsCategory`` (or list of them); call at import time."""
    try:
        register_callback("register_settings", lambda: settings)
    except ValueError:
        logger.debug("Core predates register_settings; /set can't list %r", settings)
