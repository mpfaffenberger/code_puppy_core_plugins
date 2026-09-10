"""Register the attachment-to-reference bridge tool with Code Puppy."""

from __future__ import annotations

from typing import Any

from code_puppy.callbacks import register_callback

from .tool import TOOL_NAME, register_tools_callback


def _register_tools() -> list[dict[str, Any]]:
    return register_tools_callback()


def _advertise_tool(agent_name: str | None = None) -> list[str]:
    del agent_name
    return [TOOL_NAME]


register_callback("register_tools", _register_tools)
register_callback("register_agent_tools", _advertise_tool)
