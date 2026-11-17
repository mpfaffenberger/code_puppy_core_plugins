"""Turn ``/github-loop <task>`` into a disciplined coding-and-PR mission."""

from __future__ import annotations

from pathlib import Path

from code_puppy.callbacks import CustomCommandResult, register_callback
from code_puppy.i18n import add_catalog_dir, t

COMMAND_NAME = "github-loop"
_CATALOG_DIR = str(Path(__file__).with_name("locales"))


def _translate(key: str) -> str:
    """Resolve plugin text, restoring the catalog after test/runtime resets."""
    add_catalog_dir(_CATALOG_DIR)
    return t(key)


def _custom_help() -> list[tuple[str, str]]:
    return [
        (
            COMMAND_NAME,
            _translate("github_loop.help"),
        )
    ]


def _parse_task(command: str) -> str | None:
    """Extract a task, returning ``None`` for empty or help invocations."""
    parts = command.split(maxsplit=1)
    if len(parts) != 2:
        return None
    task = parts[1].strip()
    if not task or task in {"--help", "-h", "help"}:
        return None
    return task


def _build_prompt(task: str) -> str:
    """Build the model-facing workflow contract for one GitHub task."""
    return f"""Execute the **Golden GitHub Loop** for the task below.

<github-loop-task>
{task}
</github-loop-task>

Work autonomously until the task is either published as a pull request or a
specific blocker requires user input. Do not merely describe commands: use the
available repository, file, shell, and GitHub tooling.

## Safety and scope

- Confirm the repository root, current branch, remotes, and working-tree state.
- Preserve all unrelated user work. Never discard, overwrite, stash, or include
  unrelated changes.
- If the current checkout is not a safe place to work, create an isolated branch
  or worktree. After `git worktree add`, use a second shell call with `cwd` set to
  that worktree, or use `git -C`; worktree creation does not change directories.
- Keep the patch narrowly tied to the stated defect or mechanism.
- Preserve contributor commits and authorship. For someone else's PR, prefer a
  companion branch/PR rather than rewriting their branch.
- Never force-push, merge a PR, push directly to `main`, rewrite history, expose
  secrets, or mutate production resources.

## 1. Inspect

- Use `list_files` before reading or modifying repository files.
- Read repository guidance and the smallest relevant configuration surface.
- Use `grep` to find definitions, call sites, tests, and established patterns.
- Use `read_file` on the relevant slices before editing anything.
- If the task references an issue or PR, inspect its live state with `gh`; do not
  trust cached summaries when live metadata is available.

## 2. Reproduce

- Reproduce the reported failure before patching whenever possible.
- Record the exact focused command and result.
- Distinguish feature failures from pre-existing or unrelated baseline failures.
- If reproduction is impossible, explain why and establish the strongest
  deterministic validation available before changing code.

## 3. Patch

- Prefer `replace_in_file` for small existing-file changes and `create_file` only
  for genuinely new cohesive files.
- Follow existing architecture and use plugin hooks instead of editing core when
  an appropriate hook exists.
- Apply DRY, YAGNI, SOLID, and the Zen of Python without opportunistic cleanup.
- Add focused regression tests that fail for the original defect and pass for
  the intended behavior.
- Keep files under 600 lines unless splitting would damage cohesion.

## 4. Validate

- Run the focused regression tests first, then the smallest meaningful adjacent
  suite.
- Run repository-prescribed linters and format checks. Do not accept broad
  automatic rewrites of unrelated files.
- Run independent lint, format, and status checks in parallel when practical.
- Run `git diff --check` and inspect `git status --short --branch`.
- If a check fails, determine whether the patch caused it before changing code.

## 5. Inspect the final diff

- Review `git diff` and `git diff --stat` as if reviewing someone else's patch.
- Verify that every changed line belongs to this task, tests prove the behavior,
  no generated junk or secrets are present, and attribution is intact.
- Remove accidental scope before publishing.

## 6. Publish through GitHub

- Verify `gh` authentication and resolve the correct upstream repository and
  base branch.
- Commit only the intended files with a concise message and no AI co-author.
- Push the feature branch to the user's fork or authorized remote.
- Open a narrowly scoped PR with `gh pr create`, or update the existing matching
  PR instead of creating a duplicate.
- In the PR body, include the defect, root cause, fix, exact validation, and any
  confirmed baseline failures.
- Inspect the resulting PR's URL, changed files, mergeability, and checks.
- Do not claim CI passed until hosted checks actually report success.

## Final response

Report:

1. what changed and why,
2. tests and checks with exact outcomes,
3. branch and commit,
4. PR URL and current merge/check state,
5. any remaining blocker or maintainer action.
"""


def _handle_custom_command(command: str, name: str) -> CustomCommandResult | str | None:
    if name != COMMAND_NAME:
        return None

    task = _parse_task(command)
    if task is None:
        return _translate("github_loop.usage")
    return CustomCommandResult(_build_prompt(task))


register_callback("custom_command_help", _custom_help)
register_callback("custom_command", _handle_custom_command)

__all__ = [
    "COMMAND_NAME",
    "_build_prompt",
    "_custom_help",
    "_handle_custom_command",
    "_parse_task",
]
