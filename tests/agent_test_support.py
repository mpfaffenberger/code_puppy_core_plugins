"""Shared fake pydantic-ai agent for plugin tool-registration tests.

Plugins register tools with both ``@agent.tool`` and
``@agent.tool(metadata=...)``; ``FakeAgent`` accepts either form and keeps
each tool function by name so tests can call it directly.

The built-wheel tests (``test_agent_creator_skill.py``,
``test_qa_kitten_skill.py``) keep an inline copy on purpose: they run in an
isolated interpreter against the installed wheel and must not import from
``tests``.
"""

from __future__ import annotations

from typing import Any, Callable


class FakeAgent:
    """Captures ``@agent.tool``-decorated functions in ``registered``."""

    def __init__(self) -> None:
        self.registered: dict[str, Callable[..., Any]] = {}

    def tool(self, function: Callable[..., Any] | None = None, **_kwargs: Any):
        if function is None:
            return self.tool
        self.registered[function.__name__] = function
        return function
