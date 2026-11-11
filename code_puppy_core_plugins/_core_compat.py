"""Small capability checks for plugin bundles that can outpace core releases."""

from __future__ import annotations


def is_auxiliary_agent_run() -> bool:
    """Return the core auxiliary-run marker, or false on older cores."""
    try:
        from code_puppy.callbacks import is_auxiliary_agent_run as core_marker
    except ImportError:
        return False
    return core_marker()


def is_hook_retry_prompt_submit() -> bool:
    """Return the core in-turn retry marker, or false on older cores."""
    try:
        from code_puppy.callbacks import is_hook_retry_prompt_submit as core_marker
    except ImportError:
        return False
    return core_marker()


__all__ = ["is_auxiliary_agent_run", "is_hook_retry_prompt_submit"]
