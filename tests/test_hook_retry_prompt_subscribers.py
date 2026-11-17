"""In-turn hook retries must not act like fresh foreground user prompts."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from code_puppy import callbacks

from code_puppy_core_plugins import _core_compat
from code_puppy_core_plugins.claude_code_hooks import register_callbacks as hooks
from code_puppy_core_plugins.completion_notification import (
    register_callbacks as notifications,
)


@pytest.fixture
def retry_context():
    if not hasattr(callbacks, "hook_retry_prompt_submit_context"):
        pytest.skip("core does not expose hook retry prompt scope")
    return callbacks.hook_retry_prompt_submit_context


@pytest.fixture
def hook_engine(monkeypatch):
    result = SimpleNamespace(
        results=[SimpleNamespace(blocked=False, exit_code=0, stdout="shell context")]
    )
    engine = SimpleNamespace(process_event=AsyncMock(return_value=result))
    monkeypatch.setattr(hooks, "_hook_engine", engine)
    monkeypatch.setattr(hooks, "_pending_session_context", ["foreground rules"])
    return engine


async def test_retry_skips_shell_hooks_and_preserves_session_context(
    monkeypatch, retry_context, hook_engine
):
    monkeypatch.setattr(hooks, "is_auxiliary_agent_run", lambda: False)

    with retry_context():
        assert await hooks.on_user_prompt_submit_hook("retry", "main-session") is None

    hook_engine.process_event.assert_not_awaited()
    assert hooks._pending_session_context == ["foreground rules"]

    # The marker resets and the next actual user prompt still runs hooks normally.
    result = await hooks.on_user_prompt_submit_hook("next question", "main-session")
    assert result == (
        "[hook context]\nforeground rules\n\n"
        "[hook context]\nshell context\n\nnext question"
    )
    assert hooks._pending_session_context == []
    hook_engine.process_event.assert_awaited_once()
    event_name, event_data = hook_engine.process_event.call_args.args
    assert event_name == "UserPromptSubmit"
    assert event_data.tool_args == {"prompt": "next question"}
    assert event_data.context == {"session_id": "main-session"}


async def test_auxiliary_prompt_still_runs_shell_hooks_without_draining_context(
    hook_engine,
):
    if not hasattr(callbacks, "auxiliary_agent_run_context"):
        pytest.skip("core does not expose auxiliary lifecycle scope")

    with callbacks.auxiliary_agent_run_context():
        result = await hooks.on_user_prompt_submit_hook("side question", "aux-session")

    assert result == "[hook context]\nshell context\n\nside question"
    assert hooks._pending_session_context == ["foreground rules"]
    hook_engine.process_event.assert_awaited_once()


def test_retry_preserves_original_notification_prompt_through_completion(
    monkeypatch, retry_context
):
    monkeypatch.setattr(notifications, "_prompts", {})
    monkeypatch.setattr(notifications, "_run_depth", 0)
    monkeypatch.setattr(notifications, "is_enabled", lambda: True)
    monkeypatch.setattr(notifications, "_is_subagent", lambda: False)
    monkeypatch.setattr(notifications, "is_auxiliary_agent_run", lambda: False)
    monkeypatch.setattr(notifications, "get_sound", lambda: "Frog")
    worker = Mock()
    monkeypatch.setattr(notifications, "_start_notification_worker", worker)

    notifications._on_user_prompt_submit("Original question", "main-session")
    notifications._on_agent_run_start()
    with retry_context():
        assert (
            notifications._on_user_prompt_submit("Result retry", "main-session") is None
        )
        assert notifications._on_user_prompt_submit("retry", "other-session") is None

    assert notifications._prompts == {"main-session": "Original question"}
    assert notifications._run_depth == 1
    worker.assert_not_called()

    notifications._on_agent_run_end(
        session_id="main-session", success=True, response_text="Final answer"
    )
    worker.assert_called_once_with("Frog", "Original question", "Final answer")
    assert notifications._prompts == {}
    assert notifications._run_depth == 0


@pytest.mark.parametrize("retry", [False, True])
def test_retry_compatibility_marker_follows_core_scope(retry, retry_context):
    with retry_context() if retry else nullcontext():
        assert _core_compat.is_hook_retry_prompt_submit() is retry
    assert _core_compat.is_hook_retry_prompt_submit() is False


async def test_older_core_without_retry_marker_keeps_normal_prompt_behavior(
    monkeypatch, hook_engine
):
    monkeypatch.delattr(callbacks, "is_hook_retry_prompt_submit", raising=False)
    monkeypatch.setattr(hooks, "is_auxiliary_agent_run", lambda: False)
    monkeypatch.setattr(notifications, "_prompts", {})
    monkeypatch.setattr(notifications, "is_enabled", lambda: True)
    monkeypatch.setattr(notifications, "_is_subagent", lambda: False)
    monkeypatch.setattr(notifications, "is_auxiliary_agent_run", lambda: False)

    assert _core_compat.is_hook_retry_prompt_submit() is False
    result = await hooks.on_user_prompt_submit_hook("Original question", "main-session")
    assert result.startswith("[hook context]\nforeground rules\n\n")
    hook_engine.process_event.assert_awaited_once()
    assert hooks._pending_session_context == []
    notifications._on_user_prompt_submit("Original question", "main-session")
    assert notifications._prompts == {"main-session": "Original question"}
