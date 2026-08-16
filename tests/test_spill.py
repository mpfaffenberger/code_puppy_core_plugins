"""Tests for private spill storage and model-facing result bounding."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import time
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, field_validator

from code_puppy import callbacks, config
from code_puppy.agent_execution_context import executing_agent_context
from code_puppy.tools.command_runner import ShellCommandOutput
from code_puppy.tools.file_operations import ListFileOutput, ReadFileOutput
from code_puppy.tools.skills_tools import SkillActivateOutput
from code_puppy.tools.subagent_context import (
    reset_conversation_root_id,
    set_conversation_root_id,
    subagent_context,
)
from code_puppy_core_plugins.spill import register_callbacks as spill
from code_puppy_core_plugins.spill import store


def _call(tool_name: str, result):
    asyncio.run(spill._on_post_tool_call(tool_name, {}, result, 1.0))


def _string_bytes(result) -> int:
    return sum(
        len(value.encode("utf-8"))
        for _, value in (spill._result_items(result) or [])
        if isinstance(value, str)
    )


class _ErrorOutput(BaseModel):
    error: str


class _RejectingOutput(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    first: str
    second: str

    @field_validator("second")
    @classmethod
    def reject_spill_preview(cls, value: str) -> str:
        if "Full output stored at:" in value:
            raise ValueError("second field must remain verbatim")
        return value


class _ConfiguredAgent:
    def __init__(self, tools_config):
        self._tools_config = tools_config

    def get_tools_config(self):
        return self._tools_config


@pytest.fixture(autouse=True)
def _spill_root(tmp_path):
    root = tmp_path / "spills"
    config.set_value(spill.ROOT_KEY, str(root))
    spill._reset_state()
    yield root
    spill._reset_state()


def test_result_under_cap_is_untouched():
    config.set_value(spill.MAX_INLINE_KEY, "1000")
    result = {"stdout": "small", "exit_code": 0}
    original = result.copy()

    _call("browser_execute_js", result)

    assert result == original


def test_oversized_field_is_spilled_and_bounded(_spill_root):
    config.set_value(spill.MAX_INLINE_KEY, "500")
    config.set_value(spill.PREVIEW_KEY, "100")
    full_output = "head\n" + "x" * 1800 + "\ntail"
    result = {"stdout": full_output, "exit_code": 0}

    _call("browser_execute_js", result)

    replacement = result["stdout"]
    files = list(_spill_root.glob("session-*/*"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == full_output
    assert str(files[0]) in replacement
    assert "bytes omitted" in replacement
    assert "start_line/num_lines" in replacement
    assert _string_bytes(result) <= 500
    assert len(replacement.encode()) < len(full_output.encode())
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(files[0].parent.stat().st_mode) == 0o700


def test_shell_command_output_model_is_spilled_and_serializes_preview(_spill_root):
    config.set_value(spill.MAX_INLINE_KEY, "700")
    config.set_value(spill.PREVIEW_KEY, "100")
    full_output = "shell-head\n" + "x" * 5000 + "\nshell-tail"
    result = ShellCommandOutput(
        success=True,
        command="produce-lots-of-output",
        error="",
        stdout=full_output,
        stderr="",
        exit_code=0,
        execution_time=0.1,
    )

    _call("agent_run_shell_command", result)

    files = list(_spill_root.glob("session-*/*"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == full_output
    assert "Full output stored at:" in result.stdout
    assert result.model_dump()["stdout"] == result.stdout
    assert _string_bytes(result) <= 700


def test_list_files_output_model_is_spilled(_spill_root):
    config.set_value(spill.MAX_INLINE_KEY, "700")
    config.set_value(spill.PREVIEW_KEY, "100")
    full_output = "\n".join(f"file-{index}.txt" for index in range(1000))
    result = ListFileOutput(content=full_output)

    _call("list_files", result)

    files = list(_spill_root.glob("session-*/*"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == full_output
    assert "Full output stored at:" in result.content
    assert _string_bytes(result) <= 700


def test_default_skips_preserve_read_file_and_activated_skill_models(_spill_root):
    config.set_value(spill.MAX_INLINE_KEY, "500")
    read_result = ReadFileOutput(content="r" * 5000, num_tokens=1000)
    skill_result = SkillActivateOutput(
        skill_name="large-skill",
        content="s" * 5000,
        resources=[],
    )
    original_read = read_result.model_copy(deep=True)
    original_skill = skill_result.model_copy(deep=True)

    _call("read_file", read_result)
    _call("activate_skill", skill_result)

    assert read_result == original_read
    assert skill_result == original_skill
    assert not list(_spill_root.glob("session-*/*"))


def test_error_only_model_result_is_untouched(_spill_root):
    config.set_value(spill.MAX_INLINE_KEY, "500")
    result = _ErrorOutput(error="x" * 5000)
    original = result.model_copy(deep=True)

    _call("some_tool", result)

    assert result == original
    assert not list(_spill_root.glob("session-*/*"))


def test_model_assignment_failure_rolls_back_all_fields(monkeypatch):
    config.set_value(spill.MAX_INLINE_KEY, "700")
    config.set_value(spill.PREVIEW_KEY, "50")
    result = _RejectingOutput(first="a" * 3000, second="b" * 2000)
    original = result.model_copy(deep=True)
    monkeypatch.setattr(
        store,
        "save_text",
        lambda *args, **kwargs: Path("/tmp/spill-result"),
    )

    _call("some_tool", result)

    assert result == original


def test_multiple_fields_spill_largest_first_until_under_cap(monkeypatch):
    config.set_value(spill.MAX_INLINE_KEY, "700")
    config.set_value(spill.PREVIEW_KEY, "80")
    result = {"second": "b" * 1200, "largest": "a" * 2000, "small": "ok"}
    saved_contents: list[str] = []

    def recording_save(
        content: str,
        tool_name: str,
        configured_root: str | None,
        session_id: str | None = None,
    ):
        _ = tool_name, configured_root, session_id
        saved_contents.append(content)
        return Path("/tmp/spill-result")

    monkeypatch.setattr(store, "save_text", recording_save)
    _call("browser_execute_js", result)

    assert saved_contents == ["a" * 2000, "b" * 1200]
    assert "Full output stored at:" in result["largest"]
    assert "Full output stored at:" in result["second"]
    assert result["small"] == "ok"
    assert _string_bytes(result) <= 700


def test_unsupported_non_dict_result_is_untouched():
    result = "x" * 50_000
    _call("some_tool", result)
    assert result == "x" * 50_000


def test_error_only_result_is_untouched():
    result = {"error": "x" * 50_000}
    original = result.copy()
    _call("some_tool", result)
    assert result == original


def test_skipped_read_file_is_untouched():
    result = {"content": "x" * 50_000}
    original = result.copy()
    _call("read_file", result)
    assert result == original


def test_storage_failure_keeps_original_result(monkeypatch):
    config.set_value(spill.MAX_INLINE_KEY, "500")
    result = {"stdout": "x" * 5000}
    original = result.copy()

    def fail_save(*args, **kwargs):
        raise PermissionError("nope")

    monkeypatch.setattr(store, "save_text", fail_save)
    _call("some_tool", result)

    assert result == original


def test_hostile_tool_name_cannot_escape_session_directory(_spill_root):
    config.set_value(spill.MAX_INLINE_KEY, "500")
    config.set_value(spill.PREVIEW_KEY, "50")
    result = {"stdout": "x" * 5000}

    _call("../../etc/x", result)

    files = list(_spill_root.glob("session-*/*"))
    assert len(files) == 1
    session = files[0].parent.resolve()
    assert files[0].resolve().parent == session
    assert ".." not in files[0].name
    assert "/" not in files[0].name
    safe_name = store.safe_filename("../../etc/x")
    assert ".." not in safe_name
    assert "/" not in safe_name
    assert "\\" not in safe_name


def test_zero_cap_disables_plugin():
    config.set_value(spill.MAX_INLINE_KEY, "0")
    result = {"stdout": "x" * 50_000}
    original = result.copy()
    _call("some_tool", result)
    assert result == original


@pytest.mark.asyncio
async def test_agent_can_disable_spill_without_affecting_concurrent_agent(
    _spill_root,
):
    config.set_value(spill.MAX_INLINE_KEY, "500")
    config.set_value(spill.PREVIEW_KEY, "100")
    disabled_result = {"stdout": "d" * 5000}
    disabled_original = disabled_result.copy()
    enabled_result = {"stdout": "e" * 5000}

    async def call_for_agent(agent, result):
        with executing_agent_context(agent):
            await spill._on_post_tool_call("some_tool", {}, result, 1.0)

    disabled_agent = _ConfiguredAgent({"spill": {"enabled": False}})
    enabled_agent = _ConfiguredAgent({"spill": {"enabled": True}})
    await asyncio.gather(
        call_for_agent(disabled_agent, disabled_result),
        call_for_agent(enabled_agent, enabled_result),
    )

    assert disabled_result == disabled_original
    assert "Full output stored at:" in enabled_result["stdout"]
    assert len(list(_spill_root.glob("session-*/*"))) == 1


def test_agent_can_skip_selected_tool_while_spilling_other_tools(_spill_root):
    config.set_value(spill.MAX_INLINE_KEY, "500")
    config.set_value(spill.PREVIEW_KEY, "100")
    skipped_result = {"content": "s" * 5000}
    skipped_original = skipped_result.copy()
    default_skipped_result = {"content": "r" * 5000}
    default_skipped_original = default_skipped_result.copy()
    spilled_result = {"content": "p" * 5000}
    agent = _ConfiguredAgent(
        {"spill": {"skip_tools": [" custom_report ", "", 123, None]}}
    )

    with executing_agent_context(agent):
        _call("custom_report", skipped_result)
        _call("read_file", default_skipped_result)
        _call("other_tool", spilled_result)

    assert skipped_result == skipped_original
    assert default_skipped_result == default_skipped_original
    assert "Full output stored at:" in spilled_result["content"]
    assert len(list(_spill_root.glob("session-*/*"))) == 1


def test_malformed_agent_skip_tools_fails_open(_spill_root):
    config.set_value(spill.MAX_INLINE_KEY, "500")
    result = {"content": "x" * 5000}
    agent = _ConfiguredAgent({"spill": {"skip_tools": "custom_report"}})

    with executing_agent_context(agent):
        _call("custom_report", result)

    assert "Full output stored at:" in result["content"]
    assert list(_spill_root.glob("session-*/*"))


def test_invalid_agent_spill_setting_fails_open(_spill_root):
    config.set_value(spill.MAX_INLINE_KEY, "500")
    result = {"stdout": "x" * 5000}
    agent = _ConfiguredAgent({"spill": {"enabled": "false"}})

    with executing_agent_context(agent):
        _call("some_tool", result)

    assert "Full output stored at:" in result["stdout"]
    assert list(_spill_root.glob("session-*/*"))


def test_invalid_cap_falls_back_to_default(_spill_root, caplog):
    config.set_value(spill.MAX_INLINE_KEY, "definitely-not-a-number")
    result = {"stdout": "x" * (spill.DEFAULT_MAX_INLINE_BYTES + 1000)}

    _call("some_tool", result)

    assert "Full output stored at:" in result["stdout"]
    assert list(_spill_root.glob("session-*/*"))
    assert f"Invalid {spill.MAX_INLINE_KEY} value" in caplog.text


def test_multibyte_preview_respects_utf8_cap():
    config.set_value(spill.MAX_INLINE_KEY, "450")
    config.set_value(spill.PREVIEW_KEY, "101")
    result = {"stdout": "\u20ac" * 1000}

    _call("some_tool", result)

    assert _string_bytes(result) <= 450
    assert "�" in result["stdout"]


def test_tiny_cap_keeps_original_when_notice_cannot_fit():
    config.set_value(spill.MAX_INLINE_KEY, "1")
    result = {"stdout": "abc"}
    original = result.copy()

    _call("some_tool", result)

    assert result == original


@pytest.mark.asyncio
async def test_pydantic_runner_sends_spilled_model_to_next_request(
    _spill_root,
):
    from pydantic_ai import Agent
    from pydantic_ai._tool_manager import ToolManager
    from pydantic_ai.messages import (
        ModelResponse,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
    )
    from pydantic_ai.models.function import FunctionModel

    from code_puppy.pydantic_patches import patch_tool_call_callbacks

    config.set_value(spill.MAX_INLINE_KEY, str(spill.DEFAULT_MAX_INLINE_BYTES))
    config.set_value(spill.PREVIEW_KEY, str(spill.DEFAULT_PREVIEW_BYTES))
    seen_tool_return = {}

    def model_function(messages, info):
        _ = info
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("agent_run_shell_command", {})])
        seen_tool_return["content"] = returns[-1].content
        return ModelResponse(parts=[TextPart("done")])

    original_call_tool = ToolManager._call_tool
    original_get_tool_def = ToolManager.get_tool_def
    original_handle_call = ToolManager.handle_call
    patch_tool_call_callbacks()
    try:
        agent = Agent(FunctionModel(model_function))

        @agent.tool_plain
        def agent_run_shell_command() -> ShellCommandOutput:
            return ShellCommandOutput(
                success=True,
                command="generate-output",
                error="",
                stdout="x" * 50_000,
                stderr="",
                exit_code=0,
                execution_time=0.1,
            )

        run_result = await agent.run("go")
    finally:
        ToolManager._call_tool = original_call_tool
        ToolManager.get_tool_def = original_get_tool_def
        ToolManager.handle_call = original_handle_call

    content = seen_tool_return["content"]
    assert run_result.output == "done"
    assert isinstance(content, ShellCommandOutput)
    assert "Full output stored at:" in content.stdout
    assert "x" * 50_000 not in content.stdout
    assert len(list(_spill_root.glob("session-*/*"))) == 1


@pytest.mark.asyncio
async def test_slow_storage_does_not_block_event_loop(monkeypatch):
    config.set_value(spill.MAX_INLINE_KEY, "100")
    ticks = 0
    running = True

    async def ticker():
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0.005)

    def slow_failure(*args, **kwargs):
        time.sleep(0.1)
        raise OSError("slow storage failure")

    monkeypatch.setattr(store, "save_text", slow_failure)
    ticker_task = asyncio.create_task(ticker())
    await asyncio.sleep(0.01)
    before = ticks
    await spill._on_post_tool_call("some_tool", {}, {"stdout": "x" * 1000}, 1.0)
    running = False
    await ticker_task

    assert ticks - before >= 5


def test_preplanted_session_symlink_is_rejected(_spill_root, monkeypatch, tmp_path):
    session_id = "known-session"
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:12]
    outside = tmp_path / "outside"
    outside.mkdir()
    _spill_root.mkdir()
    os.symlink(outside, _spill_root / f"session-{digest}")
    monkeypatch.setattr(store, "current_session_id", lambda: session_id)

    with pytest.raises(OSError):
        store.save_text("secret", "some_tool", str(_spill_root))

    assert list(outside.iterdir()) == []


def test_async_safe_conversation_root_scopes_spills():
    token = set_conversation_root_id("conversation-root")
    try:
        assert store.current_session_id() == "conversation-root"
    finally:
        reset_conversation_root_id(token)


@pytest.mark.asyncio
async def test_concurrent_subagents_do_not_use_racy_message_bus_sessions():
    from code_puppy.messaging import set_session_context

    first_set = asyncio.Event()
    second_set = asyncio.Event()

    async def get_scope(name: str, session_id: str, wait_for, release):
        with subagent_context(name):
            set_session_context(session_id)
            release.set()
            await wait_for.wait()
            return store.current_session_id()

    try:
        first, second = await asyncio.gather(
            get_scope("first", "session-A", second_set, first_set),
            get_scope("second", "session-B", first_set, second_set),
        )
    finally:
        set_session_context(None)

    assert first == second
    assert first not in {"session-A", "session-B"}


@pytest.mark.asyncio
async def test_startup_moves_spill_after_late_result_mutators(monkeypatch):
    cap = 300
    result = {"stdout": "x" * 5000}

    def late_mutator(tool_name, tool_args, result, duration_ms, context=None):
        result["late"] = "z" * 50

    callbacks.register_callback("post_tool_call", late_mutator)
    try:
        spill._on_startup()
        registered = callbacks.get_callbacks("post_tool_call", include_disabled=True)
        assert registered[-1] is spill._on_post_tool_call

        late_mutator("some_tool", {}, result, 1.0)
        monkeypatch.setattr(
            spill,
            "_get_int",
            lambda key, default: cap if key == spill.MAX_INLINE_KEY else 80,
        )
        monkeypatch.setattr(spill, "_get_skip_tools", lambda: frozenset())
        monkeypatch.setattr(
            store,
            "save_text",
            lambda *args, **kwargs: Path("/tmp/spill-result"),
        )
        await spill._on_post_tool_call("some_tool", {}, result, 1.0)
    finally:
        callbacks.unregister_callback("post_tool_call", late_mutator)

    assert _string_bytes(result) <= cap
