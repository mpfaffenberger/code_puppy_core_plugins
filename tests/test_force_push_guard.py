"""Force-push flags must belong to the push, not a neighboring command."""

import pytest

from code_puppy_core_plugins.force_push_guard.detector import detect_force_push


def test_push_followed_by_github_label_request() -> None:
    command = (
        "cd /Users/mpfaffenberger/code/harness-filesystem-change-events && "
        "uv run --no-sync ruff format --check "
        "pydantic_ai_harness/filesystem tests/filesystem 2>&1 | tail -1 && "
        "uv run --no-sync ruff check "
        "pydantic_ai_harness/filesystem tests/filesystem 2>&1 | tail -1 && "
        "uv run --no-sync pytest -p no:cacheprovider tests/filesystem "
        "2>&1 | tail -1 && git push -q origin puppy/filesystem-change-events "
        "&& echo pushed && gh api -f 'labels[]=pydanty:review-lite' "
        "repos/pydantic/pydantic-ai-harness/issues/853/labels "
        "--jq '.[].name' | grep pydanty"
    )
    assert detect_force_push(command) is None


@pytest.mark.parametrize("separator", ["&&", "||", ";", "|", "&", "\n"])
@pytest.mark.parametrize("flag", ["-f", "-F", "--force", "--force-with-lease", "+main"])
def test_other_commands_flags_are_ignored(separator: str, flag: str) -> None:
    assert detect_force_push(f"git push origin main {separator} other {flag}") is None


@pytest.mark.parametrize(
    "command",
    [
        "echo 'git push --force'",
        "echo 'hello && git push --force'",
        'echo "hello ; git push -f"',
        "git push origin main && echo 'git push --force'",
        "git push origin main # gh api -f value",
        "echo foo \\; git push --force",
    ],
)
def test_non_commands_are_ignored(command: str) -> None:
    assert detect_force_push(command) is None


@pytest.mark.parametrize("prefix", ["", "true && ", "false || ", "echo hi\n", "("])
@pytest.mark.parametrize(
    "flag",
    ["-f", "-F", "--force", "--force-with-lease", "--force-if-includes", "+main"],
)
def test_real_force_pushes_are_detected(prefix: str, flag: str) -> None:
    match = detect_force_push(f"{prefix}git push origin {flag} && echo done")
    assert match is not None
    assert match.pattern_name == ("+refspec" if flag == "+main" else flag)


def test_quoted_separator_does_not_hide_force_flag() -> None:
    assert detect_force_push("git push 'remote;name' main --force") is not None


def test_command_after_comment_is_checked() -> None:
    assert detect_force_push("echo ok # ignored\ngit push --force") is not None
