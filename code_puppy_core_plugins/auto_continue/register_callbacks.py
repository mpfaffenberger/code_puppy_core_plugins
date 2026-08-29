"""Register automatic continuation after routine permission requests."""

from __future__ import annotations

from typing import Any

from code_puppy.callbacks import register_callback

from .agent import AutoContinueAgent

_AGENT_NAME = "auto-continue"
_APPROVALS = {"yes, go.", "continue", "okay"}


def _register_agents() -> list[dict[str, object]]:
    return [{"name": _AGENT_NAME, "class": AutoContinueAgent}]


def _response_text(result: Any) -> str | None:
    if result is None:
        return None
    output = getattr(result, "output", result)
    return output if isinstance(output, str) else None


async def _on_interactive_turn_end(
    agent: Any,
    prompt: str,
    result: Any = None,
    *,
    success: bool = True,
    error: BaseException | None = None,
) -> dict[str, str] | None:
    """Ask the classifier whether the completed response should be continued."""
    response = _response_text(result)
    if not success or error is not None or not response:
        return None

    from code_puppy.tools.subagent_invocation import _invoke_agent_impl

    verdict = await _invoke_agent_impl(
        context=None,
        agent_name=_AGENT_NAME,
        prompt=f"Classify this assistant response:\n\n{response}",
        emit_response_message=False,
    )
    approval = (verdict.response or "").strip().lower()
    if approval not in _APPROVALS:
        return None
    return {"prompt": approval}


register_callback("register_agents", _register_agents)
register_callback("interactive_turn_end", _on_interactive_turn_end)


__all__ = ["_on_interactive_turn_end", "_register_agents"]
