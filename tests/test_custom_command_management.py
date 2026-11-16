"""Command creation uses scratch directories, never the user's commands."""

from unittest.mock import Mock

import pytest

from code_puppy_core_plugins.customizable_commands import management
from code_puppy_core_plugins.customizable_commands import register_callbacks as commands


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    from code_puppy import callbacks

    monkeypatch.chdir(tmp_path)
    root = tmp_path / "global" / "commands"
    monkeypatch.setattr(commands, "_GLOBAL_COMMANDS_DIR", str(root))
    monkeypatch.setattr(
        commands, "_COMMAND_DIRECTORIES", [str(root), ".agents/commands"]
    )
    monkeypatch.setattr(commands, "_TRUSTED_EXEC_DIRECTORIES", frozenset({str(root)}))
    monkeypatch.setattr(callbacks, "on_custom_command_help", list)
    monkeypatch.setattr(management, "get_command", lambda name: name == "help")
    info, error = Mock(), Mock()
    monkeypatch.setattr(management, "emit_info", info)
    monkeypatch.setattr(management, "emit_error", error)
    commands.reload_commands()
    return root, info, error


def test_global_create_and_immediate_invocation(sandbox):
    root, _, error = sandbox
    assert (
        management.handle_commands("/commands create review Explain this diff.") is True
    )
    assert (
        root / "review.md"
    ).read_text() == "# Custom command\n\nExplain this diff.\n"
    result = commands._handle_custom_command("/review src", "review")
    assert "Explain this diff." in result.content
    assert "Additional context: src" in result.content
    assert not error.called
    assert "review" in dict(commands._custom_help())


def test_project_create(sandbox):
    root, _, error = sandbox
    management.handle_commands("/commands create --project review Explain the changes")
    assert not root.exists()
    assert commands._command_sources["review"].name == "review.md"
    assert (root.parent.parent / ".agents/commands/review.md").exists()
    assert not error.called


@pytest.mark.parametrize(
    "name",
    [
        "../oops",
        "/oops",
        "a/b",
        "_private",
        ".hidden",
        "Upper",
        "a.md",
        "a\\b",
        "a" * 65,
    ],
)
def test_invalid_names_do_not_write(sandbox, name):
    root, _, error = sandbox
    management.handle_commands(f"/commands create {name} prompt")
    assert error.called
    assert not root.exists()


@pytest.mark.parametrize(
    "command",
    [
        "/commands create",
        "/commands create review",
        "/commands create --project",
        "/commands nonsense",
        "/commands list extra",
    ],
)
def test_usage_errors_do_not_write(sandbox, command):
    root, _, error = sandbox
    assert management.handle_commands(command)
    assert error.called
    assert not root.exists()


def test_existing_command_not_overwritten(sandbox):
    root, _, error = sandbox
    root.mkdir(parents=True)
    target = root / "review.md"
    target.write_text("Original")
    management.handle_commands("/commands create review Replacement")
    assert target.read_text() == "Original"
    assert error.called


def test_registered_command_collision(sandbox):
    root, _, error = sandbox
    management.handle_commands("/commands create help prompt")
    assert error.called
    assert not root.exists()


def test_plugin_or_skill_collision(sandbox, monkeypatch):
    from code_puppy import callbacks

    root, _, error = sandbox
    monkeypatch.setattr(
        callbacks, "on_custom_command_help", lambda: [[("review", "Skill")]]
    )
    management.handle_commands("/commands create review prompt")
    assert error.called
    assert not root.exists()


def test_empty_file_not_overwritten(sandbox):
    root, _, error = sandbox
    root.mkdir(parents=True)
    target = root / "review.md"
    target.touch()
    management.handle_commands("/commands create review prompt")
    assert target.read_bytes() == b""
    assert error.called


def test_dangling_target_symlink_not_followed(sandbox):
    root, _, error = sandbox
    root.mkdir(parents=True)
    destination = root.parent / "outside"
    (root / "review.md").symlink_to(destination)
    management.handle_commands("/commands create review prompt")
    assert error.called
    assert not destination.exists()


def test_project_directory_symlink_rejected(sandbox):
    root, _, error = sandbox
    root.parent.mkdir(parents=True)
    (root.parent.parent / ".agents").symlink_to(root.parent, target_is_directory=True)
    management.handle_commands("/commands create --project review prompt")
    assert error.called
    assert not root.exists()


def test_exec_frontmatter_is_only_prompt_text(sandbox):
    _, _, error = sandbox
    management.handle_commands(
        "/commands create review ---\nexec: echo nope\n---\nExplain"
    )
    assert "review" not in commands._command_exec_directives
    assert "exec: echo nope" in commands._custom_commands["review"]
    assert not error.called


def test_list_shows_winning_source_and_refreshes(sandbox):
    root, info, _ = sandbox
    root.mkdir(parents=True)
    (root / "review.md").write_text("Global")
    project = root.parent.parent / ".agents/commands"
    project.mkdir(parents=True)
    target = project / "review.md"
    target.write_text("Project")
    management.handle_commands("/commands list")
    assert commands._custom_commands["review"] == "Project"
    assert commands._command_sources["review"] == target
    assert any(str(target) in call.args[0] for call in info.call_args_list)


def test_io_failure_is_reported(sandbox, monkeypatch):
    from pathlib import Path

    _, _, error = sandbox
    monkeypatch.setattr(Path, "mkdir", Mock(side_effect=PermissionError("denied")))
    assert management.handle_commands("/commands create review prompt")
    assert "Could not create" in error.call_args.args[0]
