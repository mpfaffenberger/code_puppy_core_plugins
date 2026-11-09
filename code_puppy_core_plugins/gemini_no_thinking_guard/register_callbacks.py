"""Wire gemini_no_thinking_guard into Code Puppy's pre_tool_call hook.

State is kept per executing agent instance (sub-agents included), in a weak map
so finished agents are dropped automatically. Nothing happens outside an agent
run or for models other than Gemini Flash.
"""

from __future__ import annotations

import logging
from typing import Any
from weakref import WeakKeyDictionary

from code_puppy.agent_execution_context import get_executing_agent
from code_puppy.callbacks import register_callback

from code_puppy_core_plugins.plugin_settings import register_settings

from .config import SETTINGS, guard_settings, is_enabled, is_gemini_flash
from .policy import RunGuard, has_thinking_since_last_prompt

logger = logging.getLogger(__name__)

_guards: WeakKeyDictionary[Any, RunGuard] = WeakKeyDictionary()


def _on_agent_run_start(*_args: Any, **_kwargs: Any) -> None:
    """A new run (user turn) on an agent starts with a clean slate."""
    agent = get_executing_agent()
    if agent is not None:
        try:
            _guards.pop(agent, None)
        except TypeError:
            pass  # not weak-referenceable, so it was never tracked


def _on_pre_tool_call(
    tool_name: str, tool_args: dict, context: Any = None
) -> dict[str, Any] | None:
    if not is_enabled():
        return None
    agent = get_executing_agent()
    if agent is None:
        return None
    try:
        if not is_gemini_flash(agent.get_model_name()):
            return None
        guard = _guards.setdefault(agent, RunGuard())
        thinking_now = has_thinking_since_last_prompt(agent.get_message_history() or [])
    except Exception:
        logger.debug(
            "gemini_no_thinking_guard: cannot inspect %r", agent, exc_info=True
        )
        return None
    _action, result = guard.before_call(tool_name, thinking_now, guard_settings())
    return result


register_settings(SETTINGS)
register_callback("agent_run_start", _on_agent_run_start)
register_callback("pre_tool_call", _on_pre_tool_call)
