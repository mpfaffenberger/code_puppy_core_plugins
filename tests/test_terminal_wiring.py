"""Registration-level tripwires for non-executed terminal integrations."""

from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_frontend_emitter_receives_blocked_terminal_through_dispatcher():
    from code_puppy import callbacks
    from code_puppy_core_plugins.frontend_emitter import register_callbacks as emitter

    if not hasattr(callbacks, "terminal_tool_observer"):
        pytest.skip("core does not support non-executed terminal observers")

    with patch.object(emitter, "emit_event") as emit:
        await callbacks.on_post_tool_call(
            "shell",
            {"command": "private"},
            {"blocked": True},
            0.0,
            {"outcome": "blocked", "execution_started": False},
        )

    emit.assert_called_once()
    event_type, payload = emit.call_args.args
    assert event_type == "tool_call_complete"
    assert payload["tool_args"] == {}
    assert payload["success"] is False


def test_auto_continue_cancel_reset_is_registered():
    from code_puppy import callbacks
    from code_puppy_core_plugins.auto_continue import (
        register_callbacks as auto_continue,
    )

    assert auto_continue._on_interactive_turn_cancel in callbacks.get_callbacks(
        "interactive_turn_cancel"
    )
