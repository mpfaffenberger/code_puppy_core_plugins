from unittest.mock import AsyncMock, patch

import pytest

from code_puppy_core_plugins.auto_continue.classifier import (
    ContinuationDecision,
    _model_name,
)
from code_puppy_core_plugins.auto_continue.register_callbacks import (
    _on_interactive_turn_end,
)


class _Result:
    output = "Want me to implement that next?"


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", ["yes, go.", "continue", "okay"])
async def test_returns_approved_continuation(approval: str):
    classifier = AsyncMock(return_value=approval)
    with patch(
        "code_puppy_core_plugins.auto_continue.register_callbacks.classify",
        classifier,
    ):
        result = await _on_interactive_turn_end(object(), "do it", _Result())

    assert result == {"prompt": approval}
    classifier.assert_awaited_once_with(_Result.output)


@pytest.mark.asyncio
async def test_ignores_rejection_and_failed_runs():
    classifier = AsyncMock(return_value=None)
    with patch(
        "code_puppy_core_plugins.auto_continue.register_callbacks.classify",
        classifier,
    ):
        rejected = await _on_interactive_turn_end(object(), "do it", _Result())
        failed = await _on_interactive_turn_end(
            object(), "do it", _Result(), success=False
        )

    assert rejected is None
    assert failed is None
    classifier.assert_awaited_once()


def test_decision_rejects_unexpected_output():
    with pytest.raises(ValueError):
        ContinuationDecision(approval="absolutely")


def test_model_name_uses_core_resolver():
    with patch(
        "code_puppy.config.get_auto_continue_model_name",
        return_value="tiny-model",
    ):
        assert _model_name() == "tiny-model"
