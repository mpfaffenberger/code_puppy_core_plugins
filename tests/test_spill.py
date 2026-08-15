"""Tests for private spill storage and model-facing result bounding."""

from __future__ import annotations

import stat

import pytest

from code_puppy import config
from code_puppy_core_plugins.spill import register_callbacks as spill
from code_puppy_core_plugins.spill import store


def _call(tool_name: str, result):
    spill._on_post_tool_call(tool_name, {}, result, 1.0)


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

    def recording_save(content: str, tool_name: str, configured_root: str | None):
        saved_contents.append(content)
        return real_save(content, tool_name, configured_root)

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
