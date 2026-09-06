"""Register opt-in desktop notifications for completed top-level agent runs."""

from __future__ import annotations

import logging
import threading
import time

from code_puppy.callbacks import register_callback

from .config import get_sound, is_enabled
from .notifier import notify_completion, prompt_preview

logger = logging.getLogger(__name__)
_WORKER_JOIN_TIMEOUT_SECONDS = 3
_lock = threading.Lock()
_run_depth = 0
_workers: set[threading.Thread] = set()
_prompts: dict[str, str] = {}


def _on_user_prompt_submit(prompt: str, session_id: str | None = None) -> None:
    """Keep an opted-in prompt only until its run finishes."""
    if not session_id or not is_enabled() or _is_subagent():
        return
    with _lock:
        _prompts[session_id] = prompt_preview(prompt)


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
    del agent_name, model_name, error, metadata
    global _run_depth
    with _lock:
        _run_depth = max(0, _run_depth - 1)
        is_outermost_run = _run_depth == 0
        prompt = _prompts.pop(session_id, "") if session_id else ""

    if (
        not is_outermost_run
        or not success
        or not response_text
        or not is_enabled()
        or _is_subagent()
    ):
        return

    try:
        _start_notification_worker(get_sound(), prompt, response_text)
    except Exception:
        logger.debug("completion notification worker could not start", exc_info=True)


def _start_notification_worker(
    sound: str, prompt: str = "", response_text: str = ""
) -> None:
    worker: threading.Thread

    def notify() -> None:
        try:
            notify_completion(sound, prompt, response_text)
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
    """Give notification workers a bounded chance to finish before exit.

    Joins share a single deadline so total wall time stays within the timeout
    regardless of how many workers are in flight, rather than N x timeout.
    """
    with _lock:
        workers = tuple(_workers)
        _workers.clear()
    deadline = time.monotonic() + _WORKER_JOIN_TIMEOUT_SECONDS
    for worker in workers:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            worker.join(timeout=remaining)
        except RuntimeError:
            logger.debug(
                "completion notification worker could not be joined", exc_info=True
            )


register_callback("user_prompt_submit", _on_user_prompt_submit)
register_callback("agent_run_start", _on_agent_run_start)
register_callback("agent_run_end", _on_agent_run_end)
register_callback("session_end", _drain_workers)
register_callback("shutdown", _drain_workers)

__all__ = [
    "_drain_workers",
    "_on_agent_run_end",
    "_on_agent_run_start",
    "_on_user_prompt_submit",
]
