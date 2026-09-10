"""Inject Pup Lint when it is installed in the current environment."""

from __future__ import annotations

from typing import Any

from code_puppy.callbacks import register_callback

from .runner import available

_TOOL_NAME = "pup_lint"


def _register_tools() -> list[dict[str, Any]]:
    if not available():
        return []
    from .tools import register_pup_lint

    return [{"name": _TOOL_NAME, "register_func": register_pup_lint}]


def _register_agent_tools(agent_name: str | None = None) -> list[str]:
    del agent_name
    return [_TOOL_NAME] if available() else []


def _load_prompt() -> str | None:
    if not available():
        return None
    return (
        "Pup Lint is available as a structured tool. After creating or changing "
        "Python files, run pup_lint on the affected paths before declaring the "
        "task complete. Address relevant diagnostics with normal file tools and "
        "rerun Pup Lint to verify the result. Pup Lint is diagnostic-only and must "
        "not replace project-specific tests."
    )


register_callback("register_tools", _register_tools)
register_callback("register_agent_tools", _register_agent_tools)
register_callback("load_prompt", _load_prompt)
