"""ACP theme catalog: the ``code_puppy/themes/list`` ext method, session meta, and change pushes.

Read-only by design: the client lists themes and reads the active one; there is no setter.

Depends on: catalog.py :: build_catalog(), read_active_theme_id(), SCHEMA_VERSION
Depends on: state.py :: get_connection()
Called by: agent.py :: CodePuppyAgent.ext_method(), new_session(), load_session(), resume_session(), on_connect(), shutdown()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from acp import RequestError

from code_puppy_core_plugins.acp import state
from code_puppy_core_plugins.theme.catalog import (
    SCHEMA_VERSION,
    build_catalog,
    read_active_theme_id,
)

logger = logging.getLogger(__name__)

EXT_LIST = "code_puppy/themes/list"
EXT_CHANGED = "code_puppy/themes/changed"
META_KEY = "codePuppy/theme"
POLL_SECONDS = 2.0

# Distinct from None: None is a valid "no active theme" observation.
_NO_BASELINE: Any = object()


async def handle_ext_method(method: str, params: Any) -> dict[str, Any]:
    """Serve the theme list; any other ext method is not found."""
    if method != EXT_LIST:
        # The wire name carries the SDK's leading underscore; the error reports it.
        raise RequestError.method_not_found("_" + method)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "active": read_active_theme_id(),
        "themes": build_catalog(),
    }


def session_meta() -> dict[str, Any]:
    """Return the active theme as ACP ``_meta`` for session responses."""
    return {META_KEY: {"active": read_active_theme_id()}}


class ThemeWatcher:
    """Polls the active theme and pushes a notification when it changes.

    The first observation is the baseline and sends nothing, so a client that
    has not yet read the list is never told about a theme it did not ask about.
    """

    def __init__(self, poll_seconds: float = POLL_SECONDS) -> None:
        self._poll_seconds = poll_seconds
        self._task: Optional[asyncio.Task[None]] = None
        self._last: Any = _NO_BASELINE

    def start(self) -> None:
        """Start the poll loop on the running event loop; no-op if already running."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._run())

    def stop(self) -> None:
        """Cancel the poll loop; safe when never started or already stopped."""
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def poll_once(self) -> None:
        """Read the active theme and notify the client if it changed since last poll."""
        # Broad catches are deliberate: a theme failure must never kill the poll loop.
        try:
            value = await asyncio.to_thread(read_active_theme_id)
        except Exception as error:
            logger.debug("ACP theme poll failed: %s", type(error).__name__)
            return
        previous, self._last = self._last, value
        if previous is _NO_BASELINE or previous == value:
            return
        connection = state.get_connection()
        if connection is None:
            return
        try:
            await connection.ext_notification(EXT_CHANGED, {"active": value})
        except Exception as error:
            logger.debug("ACP theme notify failed: %s", type(error).__name__)

    async def _run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self._poll_seconds)
