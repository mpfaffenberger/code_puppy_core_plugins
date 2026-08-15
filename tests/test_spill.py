"""Tests for private spill storage and model-facing result bounding."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import time
from pathlib import Path

import pytest

from code_puppy import callbacks, config
from code_puppy.tools.subagent_context import (
    reset_conversation_root_id,
    set_conversation_root_id,
    subagent_context,
)
from code_puppy_core_plugins.spill import register_callbacks as spill
from code_puppy_core_plugins.spill import store


def _call(tool_name: str, result):
    asyncio.run(spill._on_post_tool_call(tool_name, {}, result, 1.0))


def _string_bytes(result: dict) -> int:
    return sum(
        len(value.encode("utf-8"))
        for value in result.values()
        if isinstance(value, str)
    )


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

    _call("agent_run_shell_command", result)

    assert result == original


def test_oversized_field_is_spilled_and_bounded(_spill_root):
    config.set_value(spill.MAX_INLINE_KEY, "500")
    config.set_value(spill.PREVIEW_KEY, "100")
    full_output = "head\n" + "x" * 1800 + "\ntail"
    result = {"stdout": full_output, "exit_code": 0}

    _call("agent_run_shell_command", result)

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


def test_multiple_fields_spill_largest_first_until_under_cap(monkeypatch):
    config.set_value(spill.MAX_INLINE_KEY, "700")
    config.set_value(spill.PREVIEW_KEY, "80")
    result = {"second": "b" * 1200, "largest": "a" * 2000, "small": "ok"}
    saved_contents: list[str] = []
    real_save = store.save_text

    def recording_save(
        content: str,
        tool_name: str,
        configured_root: str | None,
        session_id: str | None = None,
    ):
        saved_contents.append(content)
        return real_save(content, tool_name, configured_root, session_id)

    monkeypatch.setattr(store, "save_text", recording_save)
    _call("agent_run_shell_command", result)

    assert saved_contents == ["a" * 2000, "b" * 1200]
    assert "Full output stored at:" in result["largest"]
    assert "Full output stored at:" in result["second"]
    assert result["small"] == "ok"
    assert _string_bytes(result) <= 700


def test_non_dict_result_is_untouched():
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
