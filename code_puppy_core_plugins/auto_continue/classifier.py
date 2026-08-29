"""Private, one-shot model classifier for automatic continuation."""

from __future__ import annotations

import asyncio
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
        from pydantic_ai import Agent, UsageLimits

        from code_puppy.model_factory import ModelFactory, make_model_settings
        from code_puppy.model_utils import prepare_prompt_for_model

        model_name = _model_name()
        if not model_name:
            return None
        models_config = ModelFactory.load_config()
        if model_name not in models_config:
            return None

        model = ModelFactory.get_model(model_name, models_config)
        prepared = prepare_prompt_for_model(
            model_name,
            _INSTRUCTIONS,
            f"Classify this assistant response:\n\n{response}",
            prepend_system_to_user=True,
        )
        agent = Agent(
            model=model,
            instructions=prepared.instructions,
            output_type=ContinuationDecision,
            retries=0,
            model_settings=make_model_settings(
                model_name,
                max_tokens=64,
                overrides=_NON_THINKING_OVERRIDES,
            ),
        )
        async with asyncio.timeout(30):
            result = await agent.run(
                prepared.user_prompt,
                usage_limits=UsageLimits(request_limit=1),
            )
    except Exception:
        return None
    return result.output.approval


__all__ = ["Approval", "ContinuationDecision", "classify"]
