"""Register automatic continuation after routine permission requests."""

from __future__ import annotations

from typing import Any

from code_puppy.callbacks import register_callback

from .classifier import classify


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

    approval = await classify(response)
    if approval is None:
        return None
    return {"prompt": approval}


register_callback("interactive_turn_end", _on_interactive_turn_end)


__all__ = ["_on_interactive_turn_end"]
