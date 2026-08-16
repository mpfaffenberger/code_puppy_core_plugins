"""No-execution adversarial tests for structured spill result inspection."""

from __future__ import annotations

from code_puppy.tools.command_runner import ShellCommandOutput

from code_puppy_core_plugins.spill.result_shapes import model_facing_mapping


def _shell_result() -> ShellCommandOutput:
    return ShellCommandOutput(
        success=True,
        command="static-gate",
        error="",
        stdout="x" * 5000,
        stderr="",
        exit_code=0,
        execution_time=0.1,
    )


def test_unknown_hostile_metaclass_is_rejected_without_hashing():
    calls = []

    class HostileMeta(type):
        def __hash__(cls):
            calls.append(cls)
            raise AssertionError("hostile metaclass hash executed")

    class UnknownResult(metaclass=HostileMeta):
        pass

    assert model_facing_mapping(UnknownResult()) is None
    assert calls == []


def test_hostile_instance_key_is_rejected_without_hashing():
    calls = []

    class HostileKey:
        def __hash__(self):
            calls.append("hash")
            return hash("stdout")

        def __eq__(self, other):
            calls.append(("eq", other))
            raise AssertionError("hostile key equality executed")

    result = _shell_result()
    raw_values = dict(object.__getattribute__(result, "__dict__"))
    raw_values[HostileKey()] = raw_values.pop("stdout")
    calls.clear()
    object.__setattr__(result, "__dict__", raw_values)

    assert model_facing_mapping(result) is None
    assert calls == []


def test_hostile_model_field_key_is_rejected_without_hashing():
    calls = []

    class HostileKey:
        def __hash__(self):
            calls.append("hash")
            return hash("stdout")

        def __eq__(self, other):
            calls.append(("eq", other))
            raise AssertionError("hostile key equality executed")

    result = _shell_result()
    fields = type(result).model_fields
    original_fields = dict(fields)
    try:
        fields[HostileKey()] = fields.pop("stdout")
        calls.clear()
        assert model_facing_mapping(result) is None
        assert calls == []
    finally:
        dict.clear(fields)
        dict.update(fields, original_fields)
