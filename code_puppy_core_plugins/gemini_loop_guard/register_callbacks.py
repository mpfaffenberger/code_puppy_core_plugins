"""Wire gemini_loop_guard into Code Puppy's tool-call hooks.

State is kept per executing agent instance (sub-agents included), in a weak
map so finished agents are dropped automatically. Nothing happens outside an
agent run or for non-Gemini models.
"""

from __future__ import annotations

import logging
from typing import Any
from weakref import WeakKeyDictionary

from code_puppy.agent_execution_context import get_executing_agent
from code_puppy.callbacks import register_callback

from code_puppy_core_plugins.plugin_settings import register_settings

from .config import SETTINGS, guard_settings, is_enabled, is_gemini_model
from .policy import RunTracker

logger = logging.getLogger(__name__)

_trackers: WeakKeyDictionary[Any, RunTracker] = WeakKeyDictionary()


def _current_tracker() -> RunTracker | None:
    """The tracker for the running Gemini agent, or ``None`` to stay out."""
    if not is_enabled():
        return None
    agent = get_executing_agent()
    if agent is None:
        return None
    try:
        if not is_gemini_model(agent.get_model_name()):
            return None
        return _trackers.setdefault(agent, RunTracker())
    except Exception:
        logger.debug("gemini_loop_guard: cannot track %r", agent, exc_info=True)
        return None


def _on_agent_run_start(*_args: Any, **_kwargs: Any) -> None:
    """A new run on an agent starts with a clean slate."""
    agent = get_executing_agent()
    if agent is not None:
        try:
            _trackers.pop(agent, None)
        except TypeError:
            pass  # not weak-referenceable, so it was never tracked


def _on_pre_tool_call(
    tool_name: str, tool_args: dict, context: Any = None
) -> dict[str, Any] | None:
    tracker = _current_tracker()
    if tracker is None:
        return None
    return tracker.before_call(tool_name, tool_args, guard_settings())


def _on_post_tool_call(
    tool_name: str,
    tool_args: dict,
    result: Any,
    duration_ms: float,
    context: Any = None,
) -> None:
    tracker = _current_tracker()
    if tracker is not None:
        tracker.record_result(tool_name, tool_args, result)


register_settings(SETTINGS)
register_callback("agent_run_start", _on_agent_run_start)
register_callback("pre_tool_call", _on_pre_tool_call)
register_callback("post_tool_call", _on_post_tool_call)
