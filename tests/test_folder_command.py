"""Tests for the /approved_folders terminal command."""

import os
import shlex

import pytest

from code_puppy_core_plugins.file_permission_handler import (
    folder_command,
    folder_policy,
)


@pytest.fixture
def messages(monkeypatch):
    captured = {"info": [], "warning": [], "error": []}
    monkeypatch.setattr(folder_command, "emit_info", captured["info"].append)
    monkeypatch.setattr(folder_command, "emit_warning", captured["warning"].append)
    monkeypatch.setattr(folder_command, "emit_error", captured["error"].append)
    return captured


@pytest.fixture
def config_store(monkeypatch, tmp_path):
    import code_puppy.config as config

    store: dict[str, str] = {}
    monkeypatch.setattr(config, "get_value", lambda key: store.get(key))
    monkeypatch.setattr(
        config, "set_config_value", lambda key, value: store.__setitem__(key, value)
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return store


def run(command: str, name: str = "approved_folders"):
    return folder_command._handle_custom_command(command, name)


def test_list_is_default_and_shows_sections(messages, config_store, tmp_path):
    assert run("/approved_folders") is True
    text = messages["info"][-1]
    assert "Project folder:" in text
    assert "Temp folders:" in text
    assert "Your folders:" in text
    assert "always ask" in text
    assert messages["error"] == []


def test_add_happy_path_saves_folder(messages, config_store, tmp_path):
    target = tmp_path / "proj"
    target.mkdir()

    assert run(f"/approved_folders add {shlex.quote(str(target))}") is True

    assert os.path.realpath(str(target)) in folder_policy.get_user_folders()
    assert "Approved folder saved" in messages["info"][-1]
    assert messages["error"] == []


def test_add_is_idempotent(messages, config_store, tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    command = f"/approved_folders add {shlex.quote(str(target))}"

    run(command)
    run(command)

    assert folder_policy.get_user_folders() == [os.path.realpath(str(target))]
    assert messages["error"] == []


@pytest.mark.parametrize(
    "path_factory, expected_fragment",
    [
        (lambda tmp: "relative/dir", "absolute"),
        (lambda tmp: str(tmp / "missing"), "exist"),
        (lambda tmp: os.path.abspath(os.sep), "root"),
        (lambda tmp: str(tmp / "home"), "home"),
    ],
    ids=["relative", "missing", "root", "home"],
)
def test_add_invalid_emits_policy_error(
    messages, config_store, tmp_path, path_factory, expected_fragment
):
    path = path_factory(tmp_path)

    assert run(f"/approved_folders add {shlex.quote(path)}") is True

    assert messages["info"] == []
    assert len(messages["error"]) == 1
    assert expected_fragment in messages["error"][0].lower()
    assert folder_policy.get_user_folders() == []


def test_remove_happy_path(messages, config_store, tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    folder_policy.add_user_folder(str(target))

    assert run(f"/approved_folders remove {shlex.quote(str(target))}") is True

    assert folder_policy.get_user_folders() == []
    assert "removed" in messages["info"][-1]
    assert messages["error"] == []


def test_remove_missing_emits_error(messages, config_store, tmp_path):
    target = tmp_path / "never-added"
    target.mkdir()

    assert run(f"/approved_folders remove {shlex.quote(str(target))}") is True

    assert len(messages["error"]) == 1
    assert messages["info"] == []


def test_protected_lists_only_protected_paths(messages, config_store):
    assert run("/approved_folders protected") is True

    text = messages["info"][-1]
    assert "Protected" in text
    assert "Project folder" not in text
    assert ".env" in text


def test_unknown_subcommand_shows_usage(messages, config_store):
    assert run("/approved_folders bogus") is True

    assert messages["warning"] == [folder_command._USAGE]
    assert messages["error"] == []


@pytest.mark.parametrize(
    "command", ["/approved_folders add", "/approved_folders remove"]
)
def test_missing_operand_shows_usage(messages, config_store, command):
    assert run(command) is True

    assert messages["warning"] == [folder_command._USAGE]


def test_other_command_name_is_not_handled(messages, config_store):
    assert run("/woof", name="woof") is None
    assert run("/approved_folders", name="") is None
    assert messages == {"info": [], "warning": [], "error": []}


def test_help_entry_content():
    assert folder_command._custom_help() == [
        (
            "approved_folders",
            "List, add or remove folders where file edits never prompt (protected paths always ask)",
        )
    ]


def test_quoted_path_with_spaces(messages, config_store, tmp_path):
    target = tmp_path / "My Docs"
    target.mkdir()

    assert run(f'/approved_folders add "{target}"') is True

    assert folder_policy.get_user_folders() == [os.path.realpath(str(target))]
    assert messages["error"] == []


def test_tilde_expands_to_home_before_add(messages, config_store, tmp_path):
    home = tmp_path / "home"
    project = home / "work"
    project.mkdir()

    assert run("/approved_folders add ~/work") is True

    assert folder_policy.get_user_folders() == [os.path.realpath(str(project))]
    assert messages["error"] == []


def test_unbalanced_quote_emits_parse_error(messages, config_store):
    assert run('/approved_folders add "unclosed') is True

    assert len(messages["error"]) == 1
    assert "parse" in messages["error"][0].lower()


def test_split_args_posix_keeps_quoted_spaces():
    assert folder_command._split_args(
        '/approved_folders add "My Docs"', posix=True
    ) == ["/approved_folders", "add", "My Docs"]


def test_split_args_windows_keeps_backslash_path_intact():
    command = r'/approved_folders add "C:\Users\me\my proj"'
    assert folder_command._split_args(command, posix=False) == [
        "/approved_folders",
        "add",
        "C:\\Users\\me\\my proj",
    ]


@pytest.mark.parametrize(
    "command, expected",
    [
        (r"add C:\Users\me\proj", ["add", "C:\\Users\\me\\proj"]),
        ("add 'C:\\a b'", ["add", "C:\\a b"]),
        ("add \"'x'\"", ["add", "'x'"]),
        ('add ""', ["add", ""]),
    ],
    ids=["unquoted-backslashes", "single-quotes", "nested-quotes", "empty-quotes"],
)
def test_split_args_windows_strips_one_matching_pair(command, expected):
    assert folder_command._split_args(command, posix=False) == expected


def test_split_args_windows_unbalanced_quote_raises():
    with pytest.raises(ValueError):
        folder_command._split_args('add "unclosed', posix=False)
