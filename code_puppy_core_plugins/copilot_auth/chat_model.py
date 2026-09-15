"""Spec-compliance shim for Copilot's non-streaming chat completions.

Copilot's ``/chat/completions`` (at least for the Claude backends) returns a
body that is *almost* OpenAI-shaped but drops two purely cosmetic fields:

- the top-level ``"object": "chat.completion"`` discriminator
- the per-choice ``"index"``

Observed body (trimmed)::

    {"choices": [{"finish_reason": "stop", "message": {...}}],
     "created": ..., "id": ..., "usage": {...}, "model": "claude-opus-5"}

The OpenAI SDK builds its ``ChatCompletion`` with ``model_construct`` (no
validation), so the missing fields silently become ``None``.  pydantic-ai then
runs a *strict* ``_ChatCompletion.model_validate(response.model_dump())`` in
``_process_response`` and blows up with ``UnexpectedModelBehavior``.

This only bites **non-streaming** calls — the streaming path parses lenient
``ChatCompletionChunk``s — which is why normal chat works but one-shot
``agent.run()`` callers (e.g. ``/compact`` summarization) die *after* the model
has already produced a perfectly good answer.

``_validate_completion`` is pydantic-ai's documented subclass hook for exactly
this, so we backfill the two fields and delegate.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.models.openai import OpenAIChatModel

_CHAT_COMPLETION_OBJECT = "chat.completion"


def backfill_openai_envelope(response: Any) -> Any:
    """Fill in ``object`` / ``choices[].index`` when Copilot omits them.

    Mutates and returns *response*.  Mutation is safe (and consistent with
    pydantic-ai, which patches ``created`` and ``finish_reason`` in place a few
    lines earlier); existing non-``None`` values are never overwritten.
    """
    if getattr(response, "object", None) is None:
        response.object = _CHAT_COMPLETION_OBJECT

    for position, choice in enumerate(getattr(response, "choices", None) or []):
        if getattr(choice, "index", None) is None:
            choice.index = position

    return response


class CopilotChatModel(OpenAIChatModel):
    """``OpenAIChatModel`` that tolerates Copilot's lossy response envelope."""

    def _validate_completion(self, response: Any) -> Any:
        return super()._validate_completion(backfill_openai_envelope(response))
