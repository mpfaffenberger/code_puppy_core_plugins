"""Regression tests for Copilot's lossy non-streaming completion envelope.

Copilot's ``/chat/completions`` omits the top-level ``object`` discriminator and
the per-choice ``index``.  The OpenAI SDK doesn't validate (``model_construct``),
so they arrive as ``None`` and pydantic-ai's strict ``_validate_completion``
rejects the whole response — killing ``/compact`` *after* the model already
produced a usable summary.
"""

from types import SimpleNamespace

import pytest

from code_puppy_core_plugins.copilot_auth.chat_model import (
    CopilotChatModel,
    backfill_openai_envelope,
)


def _copilot_response(**overrides):
    """A response shaped like Copilot's real (observed) non-streaming body."""
    payload = {
        "object": None,
        "choices": [
            SimpleNamespace(index=None, finish_reason="stop"),
            SimpleNamespace(index=None, finish_reason="stop"),
        ],
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


class TestBackfillOpenAIEnvelope:
    def test_fills_missing_object_discriminator(self):
        assert backfill_openai_envelope(_copilot_response()).object == "chat.completion"

    def test_fills_missing_choice_indexes_positionally(self):
        result = backfill_openai_envelope(_copilot_response())
        assert [choice.index for choice in result.choices] == [0, 1]

    def test_never_overwrites_values_the_provider_did_send(self):
        response = _copilot_response(
            object="chat.completion.chunk",
            choices=[SimpleNamespace(index=7, finish_reason="stop")],
        )
        result = backfill_openai_envelope(response)
        assert result.object == "chat.completion.chunk"
        assert result.choices[0].index == 7

    def test_returns_the_same_object_it_was_given(self):
        response = _copilot_response()
        assert backfill_openai_envelope(response) is response

    @pytest.mark.parametrize("choices", [None, []])
    def test_tolerates_absent_or_empty_choices(self, choices):
        result = backfill_openai_envelope(_copilot_response(choices=choices))
        assert result.object == "chat.completion"


class TestCopilotChatModel:
    def test_validate_completion_backfills_before_delegating(self, monkeypatch):
        seen = {}

        def _fake_super_validate(self, response):
            seen["object"] = response.object
            seen["indexes"] = [c.index for c in response.choices]
            return "validated"

        monkeypatch.setattr(
            "pydantic_ai.models.openai.OpenAIChatModel._validate_completion",
            _fake_super_validate,
        )

        # __new__ skips provider/network setup — we only exercise validation.
        model = CopilotChatModel.__new__(CopilotChatModel)
        assert model._validate_completion(_copilot_response()) == "validated"
        assert seen == {"object": "chat.completion", "indexes": [0, 1]}
