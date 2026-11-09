"""State-owning plugins must ignore direct auxiliary model lifecycles."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from code_puppy import callbacks
from code_puppy_core_plugins.claude_code_hooks import register_callbacks as hooks
from code_puppy_core_plugins.subagent_panel import register_callbacks as panel
from code_puppy_core_plugins.subagent_panel import state as panel_state

if not hasattr(callbacks, "auxiliary_agent_run_context"):
    pytest.skip(
        "core does not expose auxiliary lifecycle scope", allow_module_level=True
    )


async def test_auxiliary_end_skips_stop_hook(monkeypatch):
    hook_engine = MagicMock()
    hook_engine.process_event = AsyncMock()
    monkeypatch.setattr(hooks, "_hook_engine", hook_engine)

    with callbacks.auxiliary_agent_run_context():
        await hooks.on_agent_run_end_hook(
            "btw",
            "model",
            metadata={"auxiliary_model_call": True},
        )

    hook_engine.process_event.assert_not_awaited()


async def test_auxiliary_end_preserves_live_subagent_panel(monkeypatch):
    panel_state.clear()
    panel_state.register("qa-kitten-session-abc", "qa-kitten", model="m")
    stop_ticker = MagicMock()
    monkeypatch.setattr(panel, "_runtime_enabled", lambda: True)
    monkeypatch.setattr(panel, "_stop_ticker", stop_ticker)
    monkeypatch.setattr(panel, "_push_panel", lambda **_kwargs: None)

    try:
        with callbacks.auxiliary_agent_run_context():
            await panel._on_agent_run_end(
                "btw",
                "model",
                metadata={"auxiliary_model_call": True},
            )

        assert [entry["name"] for entry in panel_state.snapshot()] == ["qa-kitten"]
        stop_ticker.assert_not_called()
    finally:
        panel_state.clear()
