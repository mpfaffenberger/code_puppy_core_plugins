"""Register opt-in desktop notifications for completed top-level agent runs."""

from __future__ import annotations

import logging
import threading

from code_puppy.callbacks import register_callback

from .config import get_sound, is_enabled
from .notifier import notify_completion

logger = logging.getLogger(__name__)
_WORKER_JOIN_TIMEOUT_SECONDS = 3
_lock = threading.Lock()
_run_depth = 0
_workers: set[threading.Thread] = set()


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


def _on_agent_run_start(*_args, **_kwargs) -> None:
    """Track public lifecycle depth so internal nested runs never notify."""
    global _run_depth
    with _lock:
        _run_depth += 1


def _on_agent_run_end(
    agent_name: str | None = None,
    model_name: str | None = None,
    session_id: str | None = None,
    success: bool = True,
    error: Exception | None = None,
    response_text: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Notify only when the outermost successful textual run completes."""
    del agent_name, model_name, session_id, error, metadata
    global _run_depth
    with _lock:
        _run_depth = max(0, _run_depth - 1)
        is_outermost_run = _run_depth == 0

    if (
        not is_outermost_run
        or not success
        or not response_text
        or not is_enabled()
        or _is_subagent()
    ):
        return

    try:
        _start_notification_worker(get_sound())
    except Exception:
        logger.debug("completion notification worker could not start", exc_info=True)


def _start_notification_worker(sound: str) -> None:
    worker: threading.Thread

    def notify() -> None:
        try:
            notify_completion(sound)
        finally:
            with _lock:
                _workers.discard(worker)

    worker = threading.Thread(
        target=notify,
        daemon=True,
        name="code-puppy-completion-notification",
    )
    with _lock:
        _workers.add(worker)
    worker.start()


def _drain_workers(*_args, **_kwargs) -> None:
    """Give notification workers a bounded chance to finish before exit."""
    with _lock:
        workers = tuple(_workers)
        _workers.clear()
    for worker in workers:
        try:
            worker.join(timeout=_WORKER_JOIN_TIMEOUT_SECONDS)
        except RuntimeError:
            logger.debug(
                "completion notification worker could not be joined", exc_info=True
            )


register_callback("agent_run_start", _on_agent_run_start)
register_callback("agent_run_end", _on_agent_run_end)
register_callback("session_end", _drain_workers)
register_callback("shutdown", _drain_workers)

__all__ = ["_drain_workers", "_on_agent_run_end", "_on_agent_run_start"]
