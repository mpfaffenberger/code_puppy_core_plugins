"""Adversarial identity-race tests for read-before-write observations."""

from __future__ import annotations

import logging

import pytest

from code_puppy_core_plugins.read_before_write import register_callbacks as rbw


@pytest.fixture(autouse=True)
def _isolated_observations():
    rbw._reset_state()
    yield
    rbw._reset_state()


def _symlink(link, target):
    try:
        link.symlink_to(target)
    except OSError as exc:  # pragma: no cover - platform privilege fallback
        pytest.skip(f"symlinks unavailable: {exc}")


def test_retarget_after_successful_mutation_does_not_bless_new_target(tmp_path, caplog):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    alias = tmp_path / "alias.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    _symlink(alias, first)

    rbw._on_post_tool_call(
        "read_file",
        {"file_path": str(alias)},
        {"content": "first", "num_tokens": 1},
        1.0,
    )
    args = {"file_path": str(alias), "content": "written", "overwrite": True}
    assert rbw._on_pre_tool_call("create_file", args) is None

    first.write_text("written", encoding="utf-8")
    alias.unlink()
    alias.symlink_to(second)
    with caplog.at_level(logging.WARNING):
        rbw._on_post_tool_call("create_file", args, {"success": True}, 1.0)

    decision = rbw._on_pre_tool_call("create_file", args)
    assert decision and "hasn't been read" in decision["reason"]
    assert "recording the pre-call target" in caplog.text


def test_blocked_snapshot_cannot_poison_later_direct_post(tmp_path):
    unread = tmp_path / "unread.txt"
    direct = tmp_path / "direct.txt"
    unread.write_text("unread", encoding="utf-8")
    direct.write_text("created directly", encoding="utf-8")
    unread_args = {
        "file_path": str(unread),
        "content": "clobber",
        "overwrite": True,
    }

    decision = rbw._on_pre_tool_call("create_file", unread_args)
    assert decision and decision["blocked"] is True
    assert rbw._mutation_attempt.get() is None

    direct_args = {"file_path": str(direct)}
    rbw._on_post_tool_call("create_file", direct_args, {"success": True}, 1.0)

    assert rbw._on_pre_tool_call("replace_in_file", direct_args) is None
    assert rbw._on_pre_tool_call("create_file", unread_args)["blocked"] is True


def test_paired_posts_clear_attempt_state_even_on_failure_or_exception(
    tmp_path, monkeypatch
):
    path = tmp_path / "paired.txt"
    path.write_text("content", encoding="utf-8")
    read_args = {"file_path": str(path)}
    rbw._on_pre_tool_call("read_file", read_args)
    rbw._on_post_tool_call(
        "read_file",
        read_args,
        {"content": "content", "num_tokens": 2},
        1.0,
    )
    assert rbw._read_attempt.get() is None
    assert rbw._mutation_attempt.get() is None

    mutation_args = {"file_path": str(path), "replacements": []}
    assert rbw._on_pre_tool_call("replace_in_file", mutation_args) is None
    rbw._on_post_tool_call("replace_in_file", mutation_args, {"success": False}, 1.0)
    assert rbw._read_attempt.get() is None
    assert rbw._mutation_attempt.get() is None

    assert rbw._on_pre_tool_call("replace_in_file", mutation_args) is None

    def broken_record(*args, **kwargs):
        raise RuntimeError("post observer exploded")

    monkeypatch.setattr(rbw.policy, "record", broken_record)
    rbw._on_post_tool_call("replace_in_file", mutation_args, {"success": True}, 1.0)
    assert rbw._read_attempt.get() is None
    assert rbw._mutation_attempt.get() is None


def test_retarget_after_missing_read_does_not_mark_new_target_absent(tmp_path, caplog):
    missing = tmp_path / "missing.txt"
    existing = tmp_path / "existing.txt"
    alias = tmp_path / "alias.txt"
    existing.write_text("existing", encoding="utf-8")
    _symlink(alias, missing)

    args = {"file_path": str(alias)}
    assert rbw._on_pre_tool_call("read_file", args) is None
    alias.unlink()
    alias.symlink_to(existing)
    result = {"error": f"File {alias} does not exist"}
    with caplog.at_level(logging.WARNING):
        rbw._on_post_tool_call("read_file", args, result, 1.0)

    decision = rbw._on_pre_tool_call("replace_in_file", args)
    assert decision and "has not been read" in decision["reason"]
    assert "absent observation skipped" in caplog.text
