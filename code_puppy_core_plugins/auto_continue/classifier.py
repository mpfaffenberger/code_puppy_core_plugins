"""Private, one-shot model classifier for automatic continuation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Approval = Literal["yes, go.", "continue", "okay"]

_INSTRUCTIONS = """Decide whether the assistant is merely waiting for permission to
continue routine work the user already requested.

Approve only a clear request to continue, proceed, or perform an explicitly
described next step within the existing task. Do not approve destructive or
risky actions, credential or payment requests, ambiguous choices, requests for
new requirements, completed work, or anything needing a real human decision.
Return no approval when uncertain."""

_NON_THINKING_OVERRIDES = {
    "reasoning_effort": "none",
    "extended_thinking": "off",
    "thinking_type": "disabled",
    "thinking_enabled": False,
    "verbosity": "low",
}


class ContinuationDecision(BaseModel):
    """Strict classifier result; ``None`` means wait for the user."""

    approval: Approval | None = None


def _model_name() -> str | None:
    """Use the dedicated override when available, otherwise the global model."""
    try:
        from code_puppy.config import get_auto_continue_model_name

        return get_auto_continue_model_name()
    except ImportError:  # Compatibility with core versions before the setting.
        from code_puppy.config import get_global_model_name, get_value

        return get_value("auto_continue_model") or get_global_model_name()


async def classify(response: str) -> Approval | None:
    """Classify one response without entering Code Puppy's sub-agent runtime."""
    try:
        from code_puppy.private_inference import run_private_prompt

        model_name = _model_name()
        if not model_name:
            return None
        decision = await run_private_prompt(
            model_name=model_name,
            instructions=_INSTRUCTIONS,
            prompt=f"Classify this assistant response:\n\n{response}",
            output_type=ContinuationDecision,
            model_settings_overrides=_NON_THINKING_OVERRIDES,
            max_tokens=64,
        )
    except Exception:
        return None
    return decision.approval


__all__ = ["Approval", "ContinuationDecision", "classify"]
