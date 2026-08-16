"""Static-gate regressions for executable or hostile result shapes."""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import threading
import warnings
from pathlib import Path
from typing import Annotated

import pytest
from pydantic import BaseModel, Field, StringConstraints
from pydantic.fields import FieldInfo

from code_puppy import config
from code_puppy.tools import agent_tools
from code_puppy.tools.agent_tools import AgentInvokeOutput
from code_puppy.tools.command_runner import ShellCommandOutput
from code_puppy_core_plugins.spill import register_callbacks as spill
from code_puppy_core_plugins.spill import result_shapes


class _CountingExclude:
    def __init__(self):
        self.calls = 0

    def __call__(self, value: str) -> bool:
        _ = value
        self.calls += 1
        return False


_counting_exclude = _CountingExclude()


if hasattr(FieldInfo, "exclude_if"):

    class _ExcludeIfOutput(BaseModel):
        content: str = Field(exclude_if=_counting_exclude)

else:
    _ExcludeIfOutput = None


class _CompiledPatternOutput(BaseModel):
    content: Annotated[
        str,
        StringConstraints(pattern=re.compile(r"^(a|aa)+$")),
    ]


class _HostileString(str):
    def encode(self, *args, **kwargs):
        _ = args, kwargs
        return b""


@pytest.fixture(autouse=True)
def _spill_root(tmp_path):
    root = tmp_path / "spills"
    config.set_value(spill.ROOT_KEY, str(root))
    config.set_value(spill.MAX_INLINE_KEY, "300")
    spill._reset_state()
    yield root
    spill._reset_state()


def _call(result) -> None:
    asyncio.run(spill._on_post_tool_call("some_tool", {}, result, 1.0))


def test_allowlisted_model_rejects_hostile_internal_dict_without_execution(
    _spill_root,
):
    calls = []

    class HostileDict(dict):
        def __iter__(self):
            calls.append("iter")
            return super().__iter__()

        def __getitem__(self, key):
            calls.append("getitem")
            return super().__getitem__(key)

        def items(self):
            calls.append("items")
            return super().items()

        def copy(self):
            calls.append("copy")
            return super().copy()

    result = ShellCommandOutput(
        success=True,
        command="hostile",
        error="",
        stdout="x" * 5000,
        stderr="",
        exit_code=0,
        execution_time=0.1,
    )
    object.__setattr__(result, "__dict__", HostileDict(result.__dict__))

    _call(result)

    assert calls == []
    assert result.stdout == "x" * 5000
    assert not list(_spill_root.glob("session-*/*"))


@pytest.mark.parametrize(
    "attribute",
    ["model_dump", "model_validate", "__getattribute__"],
)
def test_allowlisted_model_rejects_late_base_model_monkeypatch(
    monkeypatch,
    attribute,
):
    calls = []
    result = ShellCommandOutput(
        success=True,
        command="contract",
        error="",
        stdout="x" * 5000,
        stderr="",
        exit_code=0,
        execution_time=0.1,
    )

    if attribute == "model_validate":

        def replacement(cls, value, **kwargs):
            calls.append((cls, value, kwargs))
            return value

        replacement = classmethod(replacement)
    elif attribute == "__getattribute__":

        def replacement(self, name):
            calls.append(name)
            return object.__getattribute__(self, name)

    else:

        def replacement(self, **kwargs):
            calls.append((self, kwargs))
            return {}

    monkeypatch.setattr(BaseModel, attribute, replacement)

    assert spill._model_facing_mapping(result) is None
    assert spill._model_validation_spec(result) is None
    assert calls == []


def test_allowlisted_model_rejects_hostile_private_state(_spill_root):
    calls = []

    class HostileSet(set):
        def __iter__(self):
            calls.append("iter")
            return super().__iter__()

    result = ShellCommandOutput(
        success=True,
        command="private-state",
        error="",
        stdout="x" * 5000,
        stderr="",
        exit_code=0,
        execution_time=0.1,
    )
    object.__setattr__(
        result,
        "__pydantic_fields_set__",
        HostileSet(result.__pydantic_fields_set__),
    )

    assert spill._model_facing_mapping(result) is None
    assert calls == []

    object.__setattr__(
        result, "__pydantic_fields_set__", set(type(result).model_fields)
    )
    object.__setattr__(result, "__pydantic_private__", {})
    assert spill._model_facing_mapping(result) is None
    assert not list(_spill_root.glob("session-*/*"))


def test_allowlisted_model_rechecks_class_contract(monkeypatch):
    result = ShellCommandOutput(
        success=True,
        command="contract",
        error="",
        stdout="x" * 5000,
        stderr="",
        exit_code=0,
        execution_time=0.1,
    )

    def custom_setattr(self, name, value):
        object.__setattr__(self, name, value)

    monkeypatch.setattr(ShellCommandOutput, "__setattr__", custom_setattr)

    assert spill._model_facing_mapping(result) is None


def test_model_mapping_never_mutates_global_warning_state(monkeypatch):
    result = ShellCommandOutput(
        success=True,
        command="safe",
        error="",
        stdout="small",
        stderr="",
        exit_code=0,
        execution_time=0.1,
    )
    original_filters = warnings.filters.copy()
    original_showwarning = warnings.showwarning

    def fail_global_capture(*args, **kwargs):
        raise AssertionError(
            "worker mapping must not use process-global warning capture"
        )

    monkeypatch.setattr(warnings, "catch_warnings", fail_global_capture)

    assert spill._model_facing_mapping(result) is not None
    assert warnings.filters == original_filters
    assert warnings.showwarning is original_showwarning


def test_older_agent_invoke_model_survives_missing_newer_subclass(monkeypatch):
    monkeypatch.delattr(agent_tools, "AgentInvokeWithModelOutput", raising=False)

    candidates = result_shapes._candidate_builtin_models()

    assert AgentInvokeOutput in candidates


def test_malformed_builtin_model_is_rejected_without_warning_or_storage(
    _spill_root,
):
    secret = "SECRET" * 1000
    result = ShellCommandOutput.model_construct(
        success=True,
        command="bad-output",
        error="",
        stdout="x" * 5000,
        stderr="",
        exit_code=secret,
        execution_time=0.1,
        timeout=False,
        user_interrupted=False,
        user_feedback=None,
        background=False,
        log_file=None,
        pid=None,
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _call(result)

    assert result.exit_code == secret
    assert caught == []
    assert not list(_spill_root.glob("session-*/*"))


@pytest.mark.skipif(_ExcludeIfOutput is None, reason="Pydantic lacks exclude_if")
def test_exclude_if_is_rejected_without_invoking_callable(_spill_root):
    assert _ExcludeIfOutput is not None
    result = _ExcludeIfOutput(content="x" * 5000)
    _counting_exclude.calls = 0

    _call(result)

    assert result.content == "x" * 5000
    assert _counting_exclude.calls == 0
    assert not list(_spill_root.glob("session-*/*"))


def test_compiled_regex_is_rejected_before_validation(_spill_root):
    content = "a" * 5000 + "!"
    result = _CompiledPatternOutput.model_construct(content=content)

    _call(result)

    assert result.content == content
    assert not list(_spill_root.glob("session-*/*"))


@pytest.mark.asyncio
async def test_supported_model_inspection_and_validation_stay_off_event_loop(
    monkeypatch,
):
    result = ShellCommandOutput(
        success=True,
        command="large-output",
        error="",
        stdout="x" * 5000,
        stderr="",
        exit_code=0,
        execution_time=0.1,
    )
    main_thread = threading.current_thread().name
    observed_threads = []
    real_mapping = spill._model_facing_mapping

    def recording_mapping(value):
        observed_threads.append(threading.current_thread().name)
        return real_mapping(value)

    monkeypatch.setattr(spill, "_model_facing_mapping", recording_mapping)
    await spill._on_post_tool_call("agent_run_shell_command", {}, result, 1.0)

    assert observed_threads
    assert main_thread not in observed_threads


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["track", "plan"])
async def test_worker_planning_exception_cleans_newly_created_file(
    _spill_root,
    monkeypatch,
    failure_point,
):
    result = {"content": "x" * 5000}

    def fail(*args, **kwargs):
        _ = args, kwargs
        raise RuntimeError(f"{failure_point} failed")

    target = spill._SpillJob if failure_point == "track" else spill
    attribute = "track" if failure_point == "track" else "_SpillPlan"
    monkeypatch.setattr(target, attribute, fail)

    await spill._on_post_tool_call("some_tool", {}, result, 1.0)

    assert result == {"content": "x" * 5000}
    assert not list(_spill_root.glob("session-*/*"))


@pytest.mark.asyncio
async def test_validation_exception_cleans_newly_created_file(
    _spill_root,
    monkeypatch,
):
    result = ShellCommandOutput(
        success=True,
        command="large-output",
        error="",
        stdout="x" * 5000,
        stderr="",
        exit_code=0,
        execution_time=0.1,
    )

    def fail_validation(*args, **kwargs):
        _ = args, kwargs
        raise RuntimeError("validation failed")

    monkeypatch.setattr(spill, "_validate_model_replacements", fail_validation)
    await spill._on_post_tool_call("agent_run_shell_command", {}, result, 1.0)

    assert result.stdout == "x" * 5000
    assert not list(_spill_root.glob("session-*/*"))


@pytest.mark.asyncio
async def test_async_commit_exception_cleans_staged_files(
    _spill_root,
    monkeypatch,
):
    result = {"content": "x" * 5000}

    def fail_commit(*args, **kwargs):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(spill, "_commit_replacements", fail_commit)
    await spill._on_post_tool_call("some_tool", {}, result, 1.0)

    assert result == {"content": "x" * 5000}
    assert not list(_spill_root.glob("session-*/*"))


def test_sync_commit_exception_cleans_staged_files(_spill_root, monkeypatch):
    result = {"content": "x" * 5000}

    def fail_commit(*args, **kwargs):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(spill, "_commit_replacements", fail_commit)
    spill._spill_result("some_tool", result, "session")

    assert result == {"content": "x" * 5000}
    assert not list(_spill_root.glob("session-*/*"))


@pytest.mark.asyncio
async def test_cancelled_worker_failure_is_drained_without_loop_error(
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    loop_errors = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

    def fail_late(*args, **kwargs):
        _ = args, kwargs
        started.set()
        assert release.wait(timeout=2)
        finished.set()
        raise RuntimeError("late worker failure")

    monkeypatch.setattr(spill.store, "save_text", fail_late)
    try:
        task = asyncio.create_task(
            spill._on_post_tool_call("some_tool", {}, {"content": "x" * 5000}, 1.0)
        )
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        for _ in range(100):
            if finished.is_set():
                break
            await asyncio.sleep(0.01)
        assert finished.is_set()
        await asyncio.sleep(0)
    finally:
        release.set()
        loop.set_exception_handler(previous_handler)

    assert loop_errors == []


def test_global_plan_can_use_multiple_minimum_notices(monkeypatch):
    config.set_value(spill.MAX_INLINE_KEY, "450")
    result = {"first": "a" * 200, "second": "b" * 200, "third": "c" * 200}
    save_calls = []

    def short_path_save(content, *args, **kwargs):
        _ = args, kwargs
        save_calls.append(content)
        return Path(f"/s/{len(save_calls)}")

    monkeypatch.setattr(spill.store, "save_text", short_path_save)
    _call(result)

    assert len(save_calls) == 2
    assert sum("Full output stored at:" in value for value in result.values()) == 2
    assert sum(len(value.encode("utf-8")) for value in result.values()) <= 450


def test_impossible_many_medium_fields_do_not_touch_storage(monkeypatch):
    config.set_value(spill.MAX_INLINE_KEY, "300")
    result = {f"field-{index}": "x" * 200 for index in range(200)}
    original = result.copy()
    save_calls = 0

    def record_save(*args, **kwargs):
        nonlocal save_calls
        _ = args, kwargs
        save_calls += 1
        raise AssertionError("globally impossible plans must fail before storage")

    monkeypatch.setattr(spill.store, "save_text", record_save)
    _call(result)

    assert result == original
    assert save_calls == 0


def test_cancellation_survives_cleanup_failure_with_saturated_default_executor(
    _spill_root,
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    real_build = spill._build_replacement
    real_cleanup = spill._cleanup_paths

    def blocked_build(*args, **kwargs):
        started.set()
        try:
            assert release.wait(timeout=2)
            return real_build(*args, **kwargs)
        finally:
            finished.set()

    def cleanup_with_failure(paths):
        if threading.current_thread().name.startswith("spill-cleanup"):
            raise OSError("cleanup failed")
        real_cleanup(paths)

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))
        task = asyncio.create_task(
            spill._on_post_tool_call("some_tool", {}, {"content": "x" * 5000}, 1.0)
        )
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)
        release.set()
        for _ in range(100):
            if finished.is_set():
                break
            await asyncio.sleep(0.01)
        assert finished.is_set()

    monkeypatch.setattr(spill, "_build_replacement", blocked_build)
    monkeypatch.setattr(spill, "_cleanup_paths", cleanup_with_failure)
    try:
        asyncio.run(scenario())
    finally:
        release.set()

    assert not list(_spill_root.glob("session-*/*"))


def test_many_tiny_fields_do_not_trigger_storage_amplification(
    _spill_root,
    monkeypatch,
):
    result = {f"field-{index}": "x" for index in range(1000)}
    original = result.copy()
    save_calls = 0

    def record_save(*args, **kwargs):
        nonlocal save_calls
        _ = args, kwargs
        save_calls += 1
        raise AssertionError("tiny fields cannot shrink enough to spill")

    monkeypatch.setattr(spill.store, "save_text", record_save)
    _call(result)

    assert result == original
    assert save_calls == 0
    assert not list(_spill_root.glob("session-*/*"))


def test_hostile_string_subclass_cannot_lie_about_size(_spill_root):
    content = _HostileString("x" * 5000)
    result = {"content": content}

    _call(result)

    assert result["content"] is content
    assert not list(_spill_root.glob("session-*/*"))
