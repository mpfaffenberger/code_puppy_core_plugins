"""Adversarial coverage for Pydantic-model spill behavior."""

from __future__ import annotations

import asyncio
import threading
from enum import StrEnum
from typing import Annotated, Literal

import pytest
from annotated_types import Predicate
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    StringConstraints,
    computed_field,
    field_serializer,
    model_serializer,
    field_validator,
    model_validator,
)
from pydantic_ai.messages import ToolReturnPart

from code_puppy import config
from code_puppy.tools.agent_tools import AgentInvokeOutput
from code_puppy.tools.command_runner import ShellCommandOutput
from code_puppy.tools.file_operations import ListFileOutput, ReadFileOutput
from code_puppy.tools.skills_tools import SkillActivateOutput
from code_puppy_core_plugins.spill import register_callbacks as spill
from code_puppy_core_plugins.spill import store

_NOTICE = "Full output stored at:"


class _ErrorOutput(BaseModel):
    error: str


class _MutableSiblingOutput(BaseModel):
    content: str
    metadata: list[str]


class _AfterValidatorOutput(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    first: str
    second: str

    @model_validator(mode="after")
    def reject_preview_after_assignment(self):
        if _NOTICE in self.second:
            raise ValueError("second field must remain verbatim")
        return self


class _ExpandingOutput(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    content: str

    @field_validator("content")
    @classmethod
    def expand_preview(cls, value: str) -> str:
        return value + ("v" * 5000) if _NOTICE in value else value


class _ExcludedSecretOutput(BaseModel):
    visible: str = "ok"
    secret: str = Field(exclude=True)


class _RedactedSecretOutput(BaseModel):
    visible: str = "ok"
    secret: str

    @field_serializer("secret")
    def redact_secret(self, value: str) -> str:
        _ = value
        return "[REDACTED]"


class _ComputedOutput(BaseModel):
    content: str = "ok"
    _calls: int = PrivateAttr(default=0)

    @computed_field
    @property
    def expanded(self) -> str:
        self._calls += 1
        return "c" * 5000


class _SecretOutput(BaseModel):
    visible: str = "ok"
    secret: SecretStr


class _ModelSerializedOutput(BaseModel):
    content: str

    @model_serializer
    def serialize_model(self):
        return {"renamed": self.content}


class _ErrorWithExcludedInternal(BaseModel):
    error: str
    internal: str = Field(exclude=True)


class _AliasedErrorOutput(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)

    problem: str = Field(serialization_alias="error")


class _ExtraErrorOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    error: str


class _FrozenOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str


class _FieldFrozenOutput(BaseModel):
    content: str = Field(frozen=True)


class _CustomSetattrOutput(BaseModel):
    content: str

    def __setattr__(self, name, value):
        super().__setattr__(name, value)


class _ThrowingDict(dict):
    def update(self, *args, **kwargs):
        self["partial"] = "mutation"
        raise RuntimeError("nope")


class _MinLengthOutput(BaseModel):
    content: str = Field(min_length=1000)


class _PatternOutput(BaseModel):
    content: Annotated[str, StringConstraints(pattern=r"^x+$")]


_LITERAL_OUTPUT = "x" * 5000


class _LiteralOutput(BaseModel):
    content: Literal[_LITERAL_OUTPUT]


class _LargeChoice(StrEnum):
    OUTPUT = "x" * 5000


class _EnumOutput(BaseModel):
    content: _LargeChoice


class _CountingPredicate:
    calls = 0

    def __call__(self, value: str) -> bool:
        self.calls += 1
        return True


_counting_predicate = _CountingPredicate()


class _PredicateOutput(BaseModel):
    content: Annotated[str, Predicate(_counting_predicate)]


class _HostileCopyOutput(BaseModel):
    content: str

    def model_copy(self, *args, **kwargs):
        _ = args, kwargs
        return self

    def __deepcopy__(self, memo):
        _ = memo
        return self


def _call(tool_name: str, result) -> None:
    asyncio.run(spill._on_post_tool_call(tool_name, {}, result, 1.0))


def _serialized_string_bytes(result) -> int:
    mapping = result.model_dump(mode="json")
    assert type(mapping) is dict
    return sum(
        len(value.encode("utf-8"))
        for value in mapping.values()
        if isinstance(value, str)
    )


@pytest.fixture(autouse=True)
def _spill_root(tmp_path):
    root = tmp_path / "spills"
    config.set_value(spill.ROOT_KEY, str(root))
    config.set_value(spill.MAX_INLINE_KEY, "700")
    config.set_value(spill.PREVIEW_KEY, "100")
    spill._reset_state()
    yield root
    spill._reset_state()


def test_shell_command_model_is_bounded_in_model_response(_spill_root):
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
    model_response = ToolReturnPart(
        tool_name="agent_run_shell_command", content=result
    ).model_response_str()
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == full_output
    assert _NOTICE in model_response
    assert full_output not in model_response
    assert _serialized_string_bytes(result) <= 700


def test_agent_invoke_model_is_spilled(_spill_root):
    full_response = "agent-response-" + "r" * 5000
    result = AgentInvokeOutput(
        response=full_response,
        agent_name="reviewer",
        session_id="session",
        model_name="model",
    )

    _call("invoke_agent", result)

    assert _NOTICE in result.response
    assert len(list(_spill_root.glob("session-*/*"))) == 1


def test_list_files_model_is_spilled(_spill_root):
    full_output = "\n".join(f"file-{index}.txt" for index in range(1000))
    result = ListFileOutput(content=full_output)

    _call("list_files", result)

    files = list(_spill_root.glob("session-*/*"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == full_output
    assert _NOTICE in result.content
    assert _serialized_string_bytes(result) <= 700


def test_nested_sibling_model_is_rejected_without_copying(_spill_root):
    result = _MutableSiblingOutput(content="x" * 5000, metadata=["same-object"])
    original_content = result.content
    original_metadata = result.metadata

    _call("some_tool", result)

    assert result.content == original_content
    assert result.metadata is original_metadata
    assert not list(_spill_root.glob("session-*/*"))


def test_default_skips_do_not_even_inspect_models(_spill_root, monkeypatch):
    read_result = ReadFileOutput(content="r" * 5000, num_tokens=1000)
    skill_result = SkillActivateOutput(
        skill_name="large-skill",
        content="s" * 5000,
        resources=[],
    )
    original_read = read_result.model_copy(deep=True)
    original_skill = skill_result.model_copy(deep=True)

    def fail_if_inspected(result):
        raise AssertionError(f"unexpected inspection of {type(result).__name__}")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("skipped tools must not create sessions or workers")

    monkeypatch.setattr(spill, "_model_facing_mapping", fail_if_inspected)
    monkeypatch.setattr(store, "current_session_id", fail_if_called)
    monkeypatch.setattr(asyncio, "to_thread", fail_if_called)
    _call("read_file", read_result)
    _call("activate_skill", skill_result)

    assert read_result == original_read
    assert skill_result == original_skill
    assert not list(_spill_root.glob("session-*/*"))


def test_error_only_model_is_untouched(_spill_root):
    result = _ErrorOutput(error="x" * 5000)
    original = result.model_copy(deep=True)

    _call("some_tool", result)

    assert result == original
    assert not list(_spill_root.glob("session-*/*"))


@pytest.mark.parametrize(
    "result",
    [
        _ExcludedSecretOutput(secret="SECRET" * 1000),
        _RedactedSecretOutput(secret="SECRET" * 1000),
        _ComputedOutput(),
        _SecretOutput(secret=SecretStr("SECRET" * 1000)),
        _ModelSerializedOutput(content="x" * 5000),
        _ErrorWithExcludedInternal(error="e" * 5000, internal="SECRET" * 1000),
        _AliasedErrorOutput(problem="e" * 5000),
        _ExtraErrorOutput(error="failed", content="x" * 5000),
    ],
    ids=[
        "excluded",
        "redacted",
        "computed",
        "secret-str",
        "model-serializer",
        "error-with-excluded",
        "alias",
        "extra",
    ],
)
def test_unsupported_serialization_shapes_are_never_persisted(_spill_root, result):
    original = result.model_copy(deep=True)
    computed_calls = getattr(result, "_calls", None)

    _call("some_tool", result)

    assert result == original
    assert getattr(result, "_calls", None) == computed_calls
    assert not list(_spill_root.glob("session-*/*"))


def test_dict_subclasses_are_not_partially_mutated(_spill_root):
    result = _ThrowingDict(content="x" * 5000)
    original = dict(result)

    _call("some_tool", result)

    assert result == original
    assert not list(_spill_root.glob("session-*/*"))


@pytest.mark.parametrize(
    "result",
    [
        _MinLengthOutput(content="x" * 5000),
        _PatternOutput(content="x" * 5000),
        _LiteralOutput(content=_LITERAL_OUTPUT),
        _EnumOutput(content=_LargeChoice.OUTPUT),
    ],
    ids=["min-length", "pattern", "literal", "str-enum"],
)
def test_schema_constraints_reject_invalid_previews(_spill_root, result):
    original_value = result.content
    original_type = type(result.content)

    _call("some_tool", result)

    assert result.content == original_value
    assert type(result.content) is original_type
    assert not list(_spill_root.glob("session-*/*"))


def test_executable_metadata_is_rejected_before_it_runs(_spill_root):
    result = _PredicateOutput(content="x" * 5000)
    _counting_predicate.calls = 0

    _call("some_tool", result)

    assert result.content == "x" * 5000
    assert _counting_predicate.calls == 0
    assert not list(_spill_root.glob("session-*/*"))


def test_custom_model_copy_overrides_are_never_used(_spill_root):
    result = _HostileCopyOutput(content="x" * 5000)

    _call("some_tool", result)

    assert result.content == "x" * 5000
    assert not list(_spill_root.glob("session-*/*"))


@pytest.mark.parametrize(
    "result",
    [
        _FrozenOutput(content="f" * 5000),
        _FieldFrozenOutput(content="f" * 5000),
        _CustomSetattrOutput(content="f" * 5000),
    ],
    ids=["frozen-model", "frozen-field", "custom-setattr"],
)
def test_non_mutable_models_are_rejected_before_storage(
    _spill_root,
    monkeypatch,
    result,
):
    original = result.model_copy(deep=True)

    def fail_if_saved(*args, **kwargs):
        raise AssertionError("storage should not run")

    monkeypatch.setattr(store, "save_text", fail_if_saved)
    _call("some_tool", result)

    assert result == original
    assert not list(_spill_root.glob("session-*/*"))


def test_after_validator_failure_never_mutates_original_or_leaks_logs(
    _spill_root,
    caplog,
):
    result = _AfterValidatorOutput(first="SECRET" * 600, second="b" * 2000)
    original = result.model_copy(deep=True)

    _call("some_tool", result)

    assert result == original
    assert not list(_spill_root.glob("session-*/*"))
    assert "SECRET" not in caplog.text
    assert _NOTICE not in caplog.text


def test_validator_expansion_over_cap_is_rejected_and_cleaned(_spill_root):
    result = _ExpandingOutput(content="x" * 5000)
    original = result.model_copy(deep=True)

    _call("some_tool", result)

    assert result == original
    assert not list(_spill_root.glob("session-*/*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dict", "model"])
async def test_stale_plan_never_overwrites_newer_result(
    _spill_root,
    monkeypatch,
    kind,
):
    result = (
        {"content": "OLD" * 2000}
        if kind == "dict"
        else ListFileOutput(content="OLD" * 2000)
    )
    started = threading.Event()
    release = threading.Event()
    real_save = store.save_text

    def blocked_save(*args, **kwargs):
        started.set()
        assert release.wait(timeout=2)
        return real_save(*args, **kwargs)

    monkeypatch.setattr(store, "save_text", blocked_save)
    task = asyncio.create_task(spill._on_post_tool_call("some_tool", {}, result, 1.0))
    assert await asyncio.to_thread(started.wait, 1)
    if kind == "dict":
        result["content"] = "NEW"
    else:
        result.content = "NEW"
    release.set()
    await task

    assert (result["content"] if kind == "dict" else result.content) == "NEW"
    assert not list(_spill_root.glob("session-*/*"))


@pytest.mark.asyncio
async def test_cancellation_never_causes_late_mutation(_spill_root, monkeypatch):
    result = ShellCommandOutput(
        success=True,
        command="produce-lots-of-output",
        error="",
        stdout="x" * 5000,
        stderr="",
        exit_code=0,
        execution_time=0.1,
    )
    original = result.model_copy(deep=True)
    started = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()
    real_save = store.save_text
    real_cleanup = spill._cleanup_paths

    def blocked_save(*args, **kwargs):
        started.set()
        assert release.wait(timeout=2)
        return real_save(*args, **kwargs)

    def recording_cleanup(paths):
        real_cleanup(paths)
        cleaned.set()

    monkeypatch.setattr(store, "save_text", blocked_save)
    monkeypatch.setattr(spill, "_cleanup_paths", recording_cleanup)
    task = asyncio.create_task(
        spill._on_post_tool_call("agent_run_shell_command", {}, result, 1.0)
    )
    assert await asyncio.to_thread(started.wait, 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.to_thread(cleaned.wait, 2)

    assert result == original
    assert not list(_spill_root.glob("session-*/*"))


def test_loop_shutdown_cancellation_cleans_worker_owned_files(
    _spill_root,
    monkeypatch,
):
    result = {"content": "x" * 5000}
    started = threading.Event()
    release = threading.Event()
    cancellation_observed = threading.Event()
    real_save = store.save_text

    def blocked_save(*args, **kwargs):
        started.set()
        assert release.wait(timeout=2)
        return real_save(*args, **kwargs)

    async def scenario():
        task = asyncio.create_task(
            spill._on_post_tool_call("some_tool", {}, result, 1.0)
        )
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cancellation_observed.set()

    def release_after_cancellation():
        assert cancellation_observed.wait(timeout=2)
        release.set()

    monkeypatch.setattr(store, "save_text", blocked_save)
    releaser = threading.Thread(target=release_after_cancellation)
    releaser.start()
    try:
        asyncio.run(scenario())
    finally:
        cancellation_observed.set()
        releaser.join(timeout=1)

    assert result == {"content": "x" * 5000}
    assert not list(_spill_root.glob("session-*/*"))
