"""Tests for version-guarded read-before-write file-tool enforcement."""

from __future__ import annotations

import logging
import os
import time
from decimal import Decimal

import pytest

from code_puppy import callbacks, config
from code_puppy_core_plugins.read_before_write import policy
from code_puppy_core_plugins.read_before_write import register_callbacks as rbw


@pytest.fixture(autouse=True)
def _isolated_observations():
    rbw._reset_state()
    yield
    rbw._reset_state()


def _pre_raw(tool_name: str, path, **tool_args):
    return rbw._on_pre_tool_call(
        tool_name,
        {"file_path": path, **tool_args},
    )


def _pre(tool_name: str, path, **tool_args):
    return _pre_raw(tool_name, str(path), **tool_args)


def _record_read(path, *, start_line: int | None = None):
    args = {"file_path": str(path)}
    if start_line is not None:
        args.update(start_line=start_line, num_lines=1)
    rbw._on_post_tool_call(
        "read_file",
        args,
        {"content": "observed", "num_tokens": 2},
        1.0,
    )


def _record_success(tool_name: str, path):
    rbw._on_post_tool_call(
        tool_name,
        {"file_path": str(path)},
        {"success": True},
        1.0,
    )


@pytest.mark.parametrize("tool_name", ["replace_in_file", "delete_snippet"])
def test_edit_without_observation_is_blocked(tool_name, tmp_path):
    path = tmp_path / "unread.txt"
    path.write_text("hello", encoding="utf-8")

    decision = _pre(tool_name, path)

    assert decision == {
        "blocked": True,
        "reason": (
            f"READ-BEFORE-WRITE: '{path}' has not been read this session. "
            "Call read_file on it first, then retry your edit."
        ),
    }


def test_ranged_read_then_edit_is_allowed(tmp_path):
    path = tmp_path / "observed.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")

    _record_read(path, start_line=2)

    assert _pre("replace_in_file", path) is None


def test_core_read_result_model_records_observation(tmp_path):
    from code_puppy.tools.file_operations import ReadFileOutput

    path = tmp_path / "pydantic-result.txt"
    path.write_text("observed", encoding="utf-8")
    result = ReadFileOutput(content="observed", num_tokens=2)

    rbw._on_post_tool_call("read_file", {"file_path": str(path)}, result, 1.0)

    assert _pre("replace_in_file", path) is None


def test_edit_after_successful_own_write_is_allowed(tmp_path):
    path = tmp_path / "owned.txt"
    path.write_text("first", encoding="utf-8")
    _record_read(path)
    assert _pre("replace_in_file", path) is None

    path.write_text("second version", encoding="utf-8")
    _record_success("replace_in_file", path)

    assert _pre("delete_snippet", path) is None


def test_observed_absent_then_edit_has_not_found_guidance(tmp_path):
    path = tmp_path / "missing.txt"
    rbw._on_post_tool_call(
        "read_file",
        {"file_path": str(path)},
        {"error": f"File {path} does not exist"},
        1.0,
    )

    decision = _pre("replace_in_file", path)

    assert decision == {
        "blocked": True,
        "reason": (
            f"'{path}' does not exist (you observed it missing earlier). "
            "Check the path with list_files or grep."
        ),
    }


def test_external_change_after_read_is_stale(tmp_path):
    path = tmp_path / "stale.txt"
    path.write_text("before", encoding="utf-8")
    _record_read(path)
    observed_stat = path.stat()

    path.write_text("after, with a different size", encoding="utf-8")
    bumped_mtime = max(time.time_ns(), observed_stat.st_mtime_ns + 1_000_000)
    os.utime(path, ns=(observed_stat.st_atime_ns, bumped_mtime))

    decision = _pre("replace_in_file", path)

    assert decision == {
        "blocked": True,
        "reason": (
            f"STALE READ: '{path}' changed on disk since you last read it "
            "(external edit?). Call read_file again before editing."
        ),
    }


def test_create_overwrite_guards_only_clobbers(tmp_path):
    existing = tmp_path / "existing.txt"
    existing.write_text("keep", encoding="utf-8")
    missing = tmp_path / "new.txt"

    existing_decision = _pre("create_file", existing, overwrite=True)

    assert existing_decision == {
        "blocked": True,
        "reason": (
            f"'{existing}' already exists but hasn't been read this session. "
            "Call read_file first (or use replace_in_file for a targeted edit)."
        ),
    }
    assert _pre("create_file", missing, overwrite=True) is None
    assert _pre("create_file", existing, overwrite=False) is None
    assert _pre("create_file", existing, overwrite="false") is None
    assert _pre("create_file", existing, overwrite="yes")["blocked"] is True


def test_create_overwrite_uses_observed_version(tmp_path):
    path = tmp_path / "overwrite.txt"
    path.write_text("original", encoding="utf-8")
    _record_read(path)
    assert _pre("create_file", path, overwrite=True) is None

    path.write_text("external change", encoding="utf-8")

    decision = _pre("create_file", path, overwrite=True)
    assert decision and "STALE READ" in decision["reason"]
    # A no-clobber create remains delegated to the tool even with stale state.
    assert _pre("create_file", path, overwrite=False) is None


def test_create_overwrite_allows_target_deleted_since_read(tmp_path):
    path = tmp_path / "recreate.txt"
    path.write_text("observed", encoding="utf-8")
    _record_read(path)
    path.unlink()

    assert _pre("create_file", path, overwrite=True) is None


def test_absent_read_then_successful_create_allows_edit(tmp_path):
    path = tmp_path / "created-after-read.txt"
    rbw._on_post_tool_call(
        "read_file",
        {"file_path": str(path)},
        {"error": f"File {path} does not exist"},
        1.0,
    )
    assert _pre("create_file", path, overwrite=False) is None

    path.write_text("new content", encoding="utf-8")
    _record_success("create_file", path)

    assert _pre("replace_in_file", path) is None


def test_successful_delete_snippet_refreshes_version(tmp_path):
    path = tmp_path / "snippet.txt"
    path.write_text("keep remove", encoding="utf-8")
    _record_read(path)
    assert _pre("delete_snippet", path) is None

    path.write_text("keep ", encoding="utf-8")
    _record_success("delete_snippet", path)

    assert _pre("replace_in_file", path) is None


def test_successful_guarded_write_updates_recorded_version(tmp_path):
    path = tmp_path / "twice.txt"
    path.write_text("version one", encoding="utf-8")
    _record_read(path)
    assert _pre("replace_in_file", path) is None

    path.write_text("version two is longer", encoding="utf-8")
    _record_success("replace_in_file", path)

    normalized = policy._normalize_path(path)
    observation = rbw._observations[rbw._scope_key()][normalized]
    current = path.stat()
    assert observation.version == (current.st_mtime_ns, current.st_size)
    assert _pre("replace_in_file", path) is None


@pytest.mark.parametrize("change", ["mtime", "size"])
def test_each_version_component_independently_detects_stale_read(tmp_path, change):
    path = tmp_path / f"stale-{change}.txt"
    path.write_text("same-size", encoding="utf-8")
    _record_read(path)
    observed = path.stat()

    if change == "mtime":
        path.write_text("new-value", encoding="utf-8")
        os.utime(
            path,
            ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000),
        )
    else:
        path.write_text("different-size", encoding="utf-8")
        os.utime(path, ns=(observed.st_atime_ns, observed.st_mtime_ns))

    decision = _pre("replace_in_file", path)
    assert decision and "STALE READ" in decision["reason"]


def test_delete_success_records_absent_without_guarding_delete(tmp_path):
    path = tmp_path / "delete-me.txt"
    path.write_text("bye", encoding="utf-8")

    assert _pre("delete_file", path) is None
    path.unlink()
    _record_success("delete_file", path)

    decision = _pre("delete_snippet", path)
    assert decision and "observed it missing" in decision["reason"]


def test_non_file_tools_and_reads_pass_through(tmp_path):
    path = tmp_path / "anything.txt"

    assert _pre("read_file", path) is None
    assert (
        rbw._on_pre_tool_call("agent_run_shell_command", {"command": "echo hi"}) is None
    )


def test_observations_are_isolated_by_conversation_and_subagent(tmp_path, monkeypatch):
    path = tmp_path / "scoped.txt"
    path.write_text("scope", encoding="utf-8")
    active = {"root": "conversation-a", "chain": ()}
    monkeypatch.setattr(rbw, "get_conversation_root_id", lambda: active["root"])
    monkeypatch.setattr(rbw, "get_subagent_chain", lambda: active["chain"])

    _record_read(path)
    assert _pre("replace_in_file", path) is None

    active["root"] = "conversation-b"
    assert _pre("replace_in_file", path)["blocked"] is True

    active["root"] = "conversation-a"
    active["chain"] = ("reviewer",)
    assert _pre("replace_in_file", path)["blocked"] is True

    active["chain"] = ()
    assert _pre("replace_in_file", path) is None


def test_config_disabled_allows_guarded_operations(tmp_path):
    path = tmp_path / "disabled.txt"
    path.write_text("unread", encoding="utf-8")
    config.set_value(rbw.ENABLED_CONFIG_KEY, "false")

    assert _pre("replace_in_file", path) is None
    assert _pre("delete_snippet", path) is None
    assert _pre("create_file", path, overwrite=True) is None


def test_config_disabled_still_records_observations(tmp_path):
    path = tmp_path / "recorded-while-disabled.txt"
    path.write_text("content", encoding="utf-8")
    config.set_value(rbw.ENABLED_CONFIG_KEY, "0")

    _record_read(path)
    config.set_value(rbw.ENABLED_CONFIG_KEY, "1")

    assert _pre("replace_in_file", path) is None


def test_unexpected_stat_error_fails_open_and_warns(tmp_path, monkeypatch, caplog):
    path = tmp_path / "stat-error.txt"
    path.write_text("content", encoding="utf-8")
    _record_read(path)

    def broken_stat(*args, **kwargs):
        raise OSError("surprise stat failure")

    # Keep the config read from consuming the mocked stat failure first; this
    # test targets the version check itself.
    monkeypatch.setattr(rbw, "_is_enabled", lambda: True)
    monkeypatch.setattr(policy.os, "stat", broken_stat)
    with caplog.at_level(logging.WARNING):
        decision = _pre("replace_in_file", path)

    assert decision is None
    assert "failed open" in caplog.text


def test_recording_stat_error_is_best_effort_and_warns(tmp_path, monkeypatch, caplog):
    path = tmp_path / "post-stat-error.txt"
    path.write_text("content", encoding="utf-8")

    def broken_stat(*args, **kwargs):
        raise OSError("post-hook stat failure")

    monkeypatch.setattr(policy.os, "stat", broken_stat)
    with caplog.at_level(logging.WARNING):
        _record_read(path)

    assert "while recording a file observation" in caplog.text
    assert rbw._observations.get(rbw._scope_key(), {}) == {}


def test_callbacks_are_registered():
    assert rbw._on_pre_tool_call in callbacks.get_callbacks(
        "pre_tool_call", include_disabled=True
    )
    assert rbw._on_post_tool_call in callbacks.get_callbacks(
        "post_tool_call", include_disabled=True
    )


def test_yolo_mode_does_not_bypass_guard(tmp_path):
    path = tmp_path / "yolo-unread.txt"
    path.write_text("content", encoding="utf-8")
    previous = config.get_cli_yolo_override()
    config.set_cli_yolo_override(True)
    try:
        decision = _pre("replace_in_file", path)
    finally:
        config.set_cli_yolo_override(previous)

    assert decision and decision["blocked"] is True


@pytest.mark.parametrize(
    "raw_path_factory",
    [os.fsencode, lambda path: bytearray(os.fsencode(path))],
)
def test_raw_paths_use_downstream_pydantic_coercion(tmp_path, raw_path_factory):
    path = tmp_path / "coerced-path.txt"
    path.write_text("content", encoding="utf-8")

    decision = _pre_raw("replace_in_file", raw_path_factory(path))

    assert decision and decision["blocked"] is True
    assert str(path) in decision["reason"]


def test_raw_overwrite_uses_downstream_pydantic_coercion(tmp_path):
    path = tmp_path / "decimal-overwrite.txt"
    path.write_text("content", encoding="utf-8")

    assert _pre_raw("create_file", str(path), overwrite=Decimal(1))["blocked"]
    # Path objects fail the downstream ``str`` validator, so policy lets the
    # normal validation error through instead of inventing a policy denial.
    assert _pre_raw("replace_in_file", path) is None


def test_session_working_directory_is_used_instead_of_process_cwd(
    tmp_path, monkeypatch
):
    from code_puppy.tools.common import reset_working_directory, set_working_directory

    process_cwd = tmp_path / "process-cwd"
    workspace = tmp_path / "workspace"
    process_cwd.mkdir()
    workspace.mkdir()
    shadow = process_cwd / "relative.txt"
    target = workspace / "relative.txt"
    shadow.write_text("shadow", encoding="utf-8")
    target.write_text("workspace", encoding="utf-8")
    monkeypatch.chdir(process_cwd)
    token = set_working_directory(str(workspace))
    try:
        _record_read("relative.txt")
        target.write_text("workspace changed", encoding="utf-8")
        decision = _pre("replace_in_file", "relative.txt")
    finally:
        reset_working_directory(token)

    assert decision and "STALE READ" in decision["reason"]


def test_tilde_path_resolves_like_file_tools(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    target = home / "tilde.txt"
    target.write_text("content", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    _record_read("~/tilde.txt")

    assert _pre("replace_in_file", "~/tilde.txt") is None
    assert set(rbw._observations[rbw._scope_key()]) == {os.path.realpath(target)}


def test_hook_context_wrapped_mutation_result_refreshes_version(tmp_path):
    path = tmp_path / "context-mutation.txt"
    path.write_text("first", encoding="utf-8")
    _record_read(path)
    path.write_text("second version", encoding="utf-8")
    wrapped = "[hook context]\nemoji_filter changed args\n\n{'success': True}"

    rbw._on_post_tool_call(
        "replace_in_file",
        {"file_path": str(path)},
        wrapped,
        1.0,
    )

    assert _pre("replace_in_file", path) is None


def test_hook_context_wrapped_read_model_records_present_and_absent(tmp_path):
    from code_puppy.tools.file_operations import ReadFileOutput

    present = tmp_path / "wrapped-present.txt"
    present.write_text("content", encoding="utf-8")
    present_args = {"file_path": str(present)}
    rbw._on_pre_tool_call("read_file", present_args)
    present_result = ReadFileOutput(content="content", num_tokens=2)
    rbw._on_post_tool_call(
        "read_file",
        present_args,
        f"[hook context]\nhook note\n\n{present_result}",
        1.0,
    )
    assert _pre("replace_in_file", present) is None

    missing = tmp_path / "wrapped-missing.txt"
    missing_args = {"file_path": str(missing)}
    error = f"File {missing} does not exist"
    rbw._on_pre_tool_call("read_file", missing_args)
    missing_result = ReadFileOutput(content=error, num_tokens=0, error=error)
    rbw._on_post_tool_call(
        "read_file",
        missing_args,
        f"[hook context]\nhook note\n\n{missing_result}",
        1.0,
    )
    assert "observed it missing" in _pre("replace_in_file", missing)["reason"]


def test_changed_file_during_read_is_not_blessed(tmp_path, caplog):
    path = tmp_path / "read-race.txt"
    path.write_text("version one", encoding="utf-8")
    args = {"file_path": str(path)}
    assert rbw._on_pre_tool_call("read_file", args) is None
    path.write_text("version two is different", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        rbw._on_post_tool_call(
            "read_file",
            args,
            {"content": "version one", "num_tokens": 3},
            1.0,
        )

    decision = _pre("replace_in_file", path)
    assert decision and "READ-BEFORE-WRITE" in decision["reason"]
    assert "changed while read_file was running" in caplog.text


def test_unrelated_not_found_text_does_not_record_absence(tmp_path):
    path = tmp_path / "backend-error.txt"
    path.write_text("content", encoding="utf-8")

    rbw._on_post_tool_call(
        "read_file",
        {"file_path": str(path)},
        {"error": "Backend dependency file not found"},
        1.0,
    )

    decision = _pre("replace_in_file", path)
    assert decision and "has not been read" in decision["reason"]


def test_missing_or_invalid_path_is_not_guarded():
    assert rbw._on_pre_tool_call("replace_in_file", {}) is None
    assert rbw._on_pre_tool_call("replace_in_file", {"file_path": None}) is None
    assert (
        rbw._on_pre_tool_call("replace_in_file", {"file_path": "bad\x00path"}) is None
    )


def test_symlink_retargeted_during_read_is_not_blessed(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    alias = tmp_path / "alias.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    try:
        alias.symlink_to(first)
    except OSError as exc:  # pragma: no cover - platform privilege fallback
        pytest.skip(f"symlinks unavailable: {exc}")

    args = {"file_path": str(alias)}
    rbw._on_pre_tool_call("read_file", args)
    alias.unlink()
    alias.symlink_to(second)
    rbw._on_post_tool_call(
        "read_file",
        args,
        {"content": "first", "num_tokens": 1},
        1.0,
    )

    decision = _pre("replace_in_file", alias)
    assert decision and "READ-BEFORE-WRITE" in decision["reason"]


def test_dotdot_and_symlink_paths_share_one_canonical_observation(tmp_path):
    directory = tmp_path / "real"
    nested = directory / "nested"
    nested.mkdir(parents=True)
    target = directory / "target.txt"
    target.write_text("canonical", encoding="utf-8")
    hostile_path = nested / ".." / "target.txt"
    symlink_path = tmp_path / "alias.txt"
    try:
        symlink_path.symlink_to(target)
    except OSError as exc:  # pragma: no cover - platform privilege fallback
        pytest.skip(f"symlinks unavailable: {exc}")

    _record_read(hostile_path)

    assert _pre("replace_in_file", symlink_path) is None
    scope_state = rbw._observations[rbw._scope_key()]
    assert set(scope_state) == {os.path.realpath(os.path.abspath(target))}
