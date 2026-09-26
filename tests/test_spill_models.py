"""Spill behavior for Pydantic BaseModel tool results."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, ConfigDict, field_validator

from code_puppy import config

from code_puppy_core_plugins.spill import register_callbacks as spill


class _ShellLike(BaseModel):
    stdout: str
    exit_code: int = 0


class _FrozenShell(BaseModel):
    model_config = ConfigDict(frozen=True)

    stdout: str


@pytest.fixture(autouse=True)
def _spill_root(tmp_path):
    root = tmp_path / "spills"
    config.set_value(spill.ROOT_KEY, str(root))
    spill._reset_state()
    yield root
    for key in (spill.ROOT_KEY, spill.MAX_INLINE_KEY, spill.PREVIEW_KEY):
        config.reset_value(key)
    spill._reset_state()


def _call(tool_name, result):
    asyncio.run(spill._on_post_tool_call(tool_name, {}, result, 0.0))


def test_oversized_model_field_is_spilled_in_place(_spill_root):
    config.set_value(spill.MAX_INLINE_KEY, "500")
    config.set_value(spill.PREVIEW_KEY, "100")
    full = "head\n" + "x" * 4000 + "\ntail"
    result = _ShellLike(stdout=full)

    _call("agent_run_shell_command", result)

    files = list(_spill_root.glob("session-*/*"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == full
    assert "bytes omitted" in result.stdout
    assert str(files[0]) in result.stdout
    assert result.exit_code == 0


def test_model_under_cap_is_untouched(_spill_root):
    config.set_value(spill.MAX_INLINE_KEY, "500")
    result = _ShellLike(stdout="x" * 100)

    _call("agent_run_shell_command", result)

    assert result.stdout == "x" * 100
    assert list(_spill_root.glob("session-*/*")) == []


def test_frozen_model_fails_open_and_stays_inline(_spill_root):
    config.set_value(spill.MAX_INLINE_KEY, "500")
    full = "x" * 4000
    result = _FrozenShell(stdout=full)

    # The rejected attribute assignment surfaces only as a debug log; the
    # model result must remain byte-identical inline.
    _call("agent_run_shell_command", result)

    assert result.stdout == full


def test_commit_replacements_restores_prior_fields_on_rejection():
    class _Rejecting(BaseModel):
        model_config = ConfigDict(validate_assignment=True)

        stdout: str
        stderr: str

        @field_validator("stderr")
        @classmethod
        def _no_previews(cls, value: str) -> str:
            if "omitted" in value:
                raise ValueError("previews not allowed")
            return value

    result = _Rejecting(stdout="a" * 50, stderr="b" * 50)

    with pytest.raises(Exception, match="previews not allowed"):
        spill._commit_replacements(
            result,
            {"stdout": "small with omitted", "stderr": "also omitted"},
        )

    assert result.stdout == "a" * 50
    assert result.stderr == "b" * 50
