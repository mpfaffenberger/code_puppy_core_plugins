"""Tests for the Golden GitHub Loop slash command."""

from code_puppy.callbacks import CustomCommandResult
from code_puppy_core_plugins.github_loop import register_callbacks as github_loop


def test_help_lists_github_loop():
    assert github_loop._custom_help() == [
        (
            "github-loop",
            "Run the inspect, patch, test, lint, diff, and GitHub PR workflow.",
        )
    ]


def test_parse_task_requires_non_help_argument():
    assert github_loop._parse_task("/github-loop") is None
    assert github_loop._parse_task("/github-loop   ") is None
    assert github_loop._parse_task("/github-loop --help") is None
    assert github_loop._parse_task("/github-loop fix issue #123") == "fix issue #123"
    assert github_loop._parse_task("/github-loop\tfix issue #123") == "fix issue #123"


def test_unowned_command_returns_none():
    assert github_loop._handle_custom_command("/other task", "other") is None


def test_empty_command_returns_display_only_usage():
    result = github_loop._handle_custom_command("/github-loop", "github-loop")

    assert isinstance(result, str)
    assert result.startswith("Usage: /github-loop <task>")


def test_task_is_forwarded_as_agent_input_with_full_workflow():
    result = github_loop._handle_custom_command(
        "/github-loop reproduce and fix issue #123", "github-loop"
    )

    assert isinstance(result, CustomCommandResult)
    prompt = result.content
    assert "reproduce and fix issue #123" in prompt
    for required_step in (
        "list_files",
        "grep",
        "read_file",
        "replace_in_file",
        "Run the focused regression tests",
        "git diff --check",
        "gh pr create",
    ):
        assert required_step in prompt


def test_core_dispatcher_forwards_workflow_to_agent():
    from code_puppy.command_line.command_handler import _dispatch_custom_command

    handled, prompt = _dispatch_custom_command(
        "/github-loop fix issue #123", "github-loop"
    )

    assert handled is True
    assert isinstance(prompt, str)
    assert "fix issue #123" in prompt
    assert "Golden GitHub Loop" in prompt


def test_workflow_protects_repository_and_publication_boundaries():
    prompt = github_loop._build_prompt("fix the bug")

    for rule in (
        "Preserve all unrelated user work",
        "Preserve contributor commits and authorship",
        "Never force-push",
        "push directly to `main`",
        "Do not claim CI passed",
        "no AI co-author",
    ):
        assert rule in prompt
