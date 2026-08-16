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

try:
    from code_puppy.agent_execution_context import executing_agent_context
except ImportError:
    executing_agent_context = None

from code_puppy.tools.subagent_context import (
    reset_conversation_root_id,
    set_conversation_root_id,
    subagent_context,
)
from code_puppy_core_plugins.spill import register_callbacks as spill
from code_puppy_core_plugins.spill import store

_requires_execution_context = pytest.mark.skipif(
    executing_agent_context is None,
    reason="requires coordinated Code Puppy execution context",
)
_requires_final_result = pytest.mark.skipif(
    not hasattr(callbacks, "on_final_tool_result"),
    reason="requires coordinated Code Puppy final-result phase",
)


def _call(tool_name: str, result):
    asyncio.run(spill._on_post_tool_call(tool_name, {}, result, 1.0))


def _string_bytes(result: dict) -> int:
    return sum(
        len(value.encode("utf-8"))
        for value in result.values()
        if isinstance(value, str)
    )


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


@pytest.mark.parametrize("failure_point", ["write", "close"])
def test_save_text_removes_partial_file_on_persistence_failure(
    _spill_root,
    monkeypatch,
    failure_point,
):
    real_fdopen = os.fdopen

    class FailingFile:
        def __init__(self, descriptor, *args, **kwargs):
            self._file = real_fdopen(descriptor, *args, **kwargs)

        def __enter__(self):
            return self

        def write(self, content):
            if failure_point == "write":
                self._file.write(content[:6])
                self._file.flush()
                raise OSError("disk full")
            return self._file.write(content)

        def __exit__(self, exc_type, exc, traceback):
            self._file.close()
            if exc_type is None and failure_point == "close":
                raise OSError("close failed")
            return False

    monkeypatch.setattr(os, "fdopen", FailingFile)

    with pytest.raises(OSError):
        store.save_text("SECRET" * 100, "some_tool", str(_spill_root), "session")

    assert not [path for path in _spill_root.rglob("*") if path.is_file()]


def test_directory_handle_close_error_does_not_misreport_persistence_failure(
    _spill_root,
    monkeypatch,
):
    real_open_session = store._open_session_dir
    real_close = os.close
    target = {}

    def recording_open(root, session_id):
        directory, descriptor = real_open_session(root, session_id)
        if descriptor is None:
            pytest.skip("platform has no directory-fd spill path")
        target["descriptor"] = descriptor
        return directory, descriptor

    def close_then_raise(descriptor):
        real_close(descriptor)
        if descriptor == target.get("descriptor"):
            raise OSError("simulated directory close error")

    monkeypatch.setattr(store, "_open_session_dir", recording_open)
    monkeypatch.setattr(os, "close", close_then_raise)

    path = store.save_text("full output", "some_tool", str(_spill_root), "session")

    assert path.read_text(encoding="utf-8") == "full output"


def test_unexpected_preview_failure_cleans_staged_file(
    _spill_root,
    monkeypatch,
):
    config.set_value(spill.MAX_INLINE_KEY, "500")
    result = {"stdout": "x" * 5000}
    original = result.copy()

    def fail_preview(*args, **kwargs):
        raise RuntimeError("preview failed")

    monkeypatch.setattr(spill, "_build_replacement", fail_preview)
    _call("some_tool", result)

    assert result == original
    assert not list(_spill_root.glob("session-*/*"))


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


@_requires_execution_context
def test_agent_disabled_returns_before_result_inspection(monkeypatch):
    result = {"stdout": "x" * 5000}
    original = result.copy()
    agent = _ConfiguredAgent({"spill": {"enabled": False}})

    def fail_if_called(*args, **kwargs):
        raise AssertionError("disabled spill must not inspect results or start work")

    monkeypatch.setattr(spill, "_model_facing_mapping", fail_if_called)
    monkeypatch.setattr(store, "current_session_id", fail_if_called)
    monkeypatch.setattr(asyncio, "to_thread", fail_if_called)
    with executing_agent_context(agent):
        _call("some_tool", result)

    assert result == original


@_requires_execution_context
def test_agent_skip_returns_before_result_inspection(monkeypatch):
    result = {"stdout": "x" * 5000}
    original = result.copy()
    agent = _ConfiguredAgent({"spill": {"skip_tools": ["some_tool"]}})

    def fail_if_called(*args, **kwargs):
        raise AssertionError("agent-skipped spill must not inspect or start work")

    monkeypatch.setattr(spill, "_model_facing_mapping", fail_if_called)
    monkeypatch.setattr(store, "current_session_id", fail_if_called)
    monkeypatch.setattr(asyncio, "to_thread", fail_if_called)
    with executing_agent_context(agent):
        _call("some_tool", result)

    assert result == original


@_requires_execution_context
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


@_requires_execution_context
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


@_requires_execution_context
def test_malformed_agent_skip_tools_fails_open(_spill_root):
    config.set_value(spill.MAX_INLINE_KEY, "500")
    result = {"content": "x" * 5000}
    agent = _ConfiguredAgent({"spill": {"skip_tools": "custom_report"}})

    with executing_agent_context(agent):
        _call("custom_report", result)

    assert "Full output stored at:" in result["content"]
    assert list(_spill_root.glob("session-*/*"))


@_requires_execution_context
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


def test_tiny_cap_keeps_original_and_removes_unused_file(_spill_root):
    config.set_value(spill.MAX_INLINE_KEY, "1")
    result = {"stdout": "abc"}
    original = result.copy()

    _call("some_tool", result)

    assert result == original
    assert not list(_spill_root.glob("session-*/*"))


@pytest.mark.asyncio
async def test_slow_result_inspection_does_not_block_event_loop(monkeypatch):
    config.set_value(spill.MAX_INLINE_KEY, "10000")
    ticks = 0
    running = True
    real_inspect = spill._model_facing_mapping

    async def ticker():
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0.005)

    def slow_inspect(result):
        time.sleep(0.1)
        return real_inspect(result)

    monkeypatch.setattr(spill, "_model_facing_mapping", slow_inspect)
    ticker_task = asyncio.create_task(ticker())
    await asyncio.sleep(0.01)
    before = ticks
    await spill._on_post_tool_call("some_tool", {}, {"stdout": "small"}, 1.0)
    running = False
    await ticker_task

    assert ticks - before >= 5


@pytest.mark.asyncio
async def test_slow_storage_does_not_block_event_loop(monkeypatch):
    config.set_value(spill.MAX_INLINE_KEY, "300")
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


def test_legacy_registration_keeps_startup_ordering_fallback(monkeypatch):
    registrations = []

    def record(phase, function, **kwargs):
        registrations.append((phase, function, kwargs))

    monkeypatch.setattr(spill, "_HAS_TERMINAL_CALLBACKS", False)
    monkeypatch.setattr(spill, "register_callback", record)

    spill._register_callbacks()

    assert registrations == [
        ("post_tool_call", spill._on_post_tool_call, {}),
        ("startup", spill._on_startup, {}),
    ]


def test_hybrid_runtime_uses_legacy_startup_reordering(monkeypatch):
    operations = []

    monkeypatch.setattr(spill, "_HAS_FINAL_TOOL_RESULT", True)
    monkeypatch.setattr(spill, "_HAS_TERMINAL_CALLBACKS", False)
    monkeypatch.setattr(
        callbacks,
        "unregister_callback",
        lambda phase, function: operations.append(("unregister", phase, function)),
    )
    monkeypatch.setattr(
        callbacks,
        "register_callback",
        lambda phase, function: operations.append(("register", phase, function)),
    )

    spill._on_startup()

    assert operations == [
        ("unregister", "post_tool_call", spill._on_post_tool_call),
        ("register", "post_tool_call", spill._on_post_tool_call),
    ]


@_requires_final_result
@pytest.mark.parametrize(
    "priority",
    [getattr(callbacks, "FINALIZER_CALLBACK_PRIORITY", 1000), 10_000_000],
)
@pytest.mark.asyncio
async def test_terminal_boundary_keeps_spill_after_public_mutators(
    _spill_root,
    priority,
):
    cap = 700
    config.set_value(spill.MAX_INLINE_KEY, str(cap))
    config.set_value(spill.PREVIEW_KEY, "80")
    result = {"stdout": "x" * 5000}

    def late_mutator(tool_name, tool_args, result, duration_ms, context=None):
        _ = tool_name, tool_args, duration_ms, context
        result["late"] = "z" * 50

    callbacks.register_callback(
        "final_tool_result",
        late_mutator,
        priority=priority,
    )
    try:
        registered = callbacks.get_callbacks("final_tool_result", include_disabled=True)
        assert registered.index(late_mutator) < registered.index(
            spill._on_post_tool_call
        )
        await callbacks.on_final_tool_result("some_tool", {}, result, 1.0)
    finally:
        callbacks.unregister_callback("final_tool_result", late_mutator)

    assert result["late"] == "z" * 50
    assert _string_bytes(result) <= cap
    assert len(list(_spill_root.glob("session-*/*"))) == 1
