"""Register opt-in desktop notifications for completed top-level agent runs."""

from __future__ import annotations

import logging
import threading

from code_puppy.callbacks import register_callback

from .config import get_sound, is_enabled
from .notifier import notify_completion

logger = logging.getLogger(__name__)


def _is_subagent() -> bool:
    try:
        from code_puppy.tools.subagent_context import is_subagent

        return is_subagent()
    except Exception:
        logger.debug(
            "sub-agent status could not be determined; suppressing notification",
            exc_info=True,
        )
        return True


def _on_agent_run_end(
    agent_name: str | None = None,
    model_name: str | None = None,
    session_id: str | None = None,
    success: bool = True,
    error: Exception | None = None,
    response_text: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Notify after a successful top-level textual response without blocking it."""
    del agent_name, model_name, session_id, error, metadata

    if not success or not response_text or not is_enabled() or _is_subagent():
        return

    try:
        threading.Thread(
            target=notify_completion,
            args=(get_sound(),),
            daemon=True,
            name="code-puppy-completion-notification",
        ).start()
    except Exception:
        logger.debug("completion notification worker could not start", exc_info=True)


register_callback("agent_run_end", _on_agent_run_end)

__all__ = ["_on_agent_run_end"]
