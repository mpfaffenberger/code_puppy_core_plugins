from unittest.mock import AsyncMock, patch

import pytest

from code_puppy_core_plugins.auto_continue.classifier import (
    ContinuationDecision,
    _model_name,
    classify,
)
from code_puppy_core_plugins.auto_continue.register_callbacks import (
    _custom_help,
    _handle_custom_command,
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
async def test_disabled_plugin_skips_classifier():
    classifier = AsyncMock()
    with (
        patch(
            "code_puppy_core_plugins.auto_continue.register_callbacks._is_enabled",
            return_value=False,
        ),
        patch(
            "code_puppy_core_plugins.auto_continue.register_callbacks.classify",
            classifier,
        ),
    ):
        result = await _on_interactive_turn_end(object(), "do it", _Result())

    assert result is None
    classifier.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_classifier_fails_open_during_model_setup():
    with patch(
        "code_puppy_core_plugins.auto_continue.classifier._model_name",
        side_effect=RuntimeError("model setup failed"),
    ):
        assert await classify(_Result.output) is None


@pytest.mark.asyncio
async def test_classifier_uses_private_non_thinking_prompt():
    decision = ContinuationDecision(approval="continue")
    private_prompt = AsyncMock(return_value=decision)
    with (
        patch(
            "code_puppy_core_plugins.auto_continue.classifier._model_name",
            return_value="tiny-model",
        ),
        patch("code_puppy.private_inference.run_private_prompt", private_prompt),
    ):
        result = await classify(_Result.output)

    assert result == "continue"
    kwargs = private_prompt.await_args.kwargs
    assert kwargs["model_name"] == "tiny-model"
    assert kwargs["model_settings_overrides"]["reasoning_effort"] == "none"
    assert kwargs["max_tokens"] == 64


def test_model_name_uses_core_resolver():
    with patch(
        "code_puppy.config.get_auto_continue_model_name",
        return_value="tiny-model",
    ):
        assert _model_name() == "tiny-model"


@pytest.mark.parametrize(("action", "enabled"), [("enable", True), ("disable", False)])
def test_toggle_command_persists_state(action: str, enabled: bool):
    with (
        patch("code_puppy.config.set_value") as set_value,
        patch("code_puppy.messaging.emit_success") as emit_success,
    ):
        handled = _handle_custom_command(f"auto-continue {action}", "auto-continue")

    assert handled is True
    set_value.assert_called_once_with(
        "auto_continue_enabled", "true" if enabled else "false"
    )
    emit_success.assert_called_once()


def test_status_command_reports_model_and_state():
    with (
        patch(
            "code_puppy_core_plugins.auto_continue.register_callbacks._is_enabled",
            return_value=True,
        ),
        patch(
            "code_puppy.config.get_auto_continue_model_name",
            return_value="tiny-model",
        ),
        patch("code_puppy.messaging.emit_info") as emit_info,
    ):
        handled = _handle_custom_command("auto-continue status", "auto-continue")

    assert handled is True
    emit_info.assert_called_once_with(
        "Auto-continue is enabled; classifier model: tiny-model."
    )


def test_command_help_and_unrelated_command():
    assert _custom_help() == [
        ("auto-continue", "Enable, disable, or inspect automatic continuation")
    ]
    assert _handle_custom_command("other", "other") is None
