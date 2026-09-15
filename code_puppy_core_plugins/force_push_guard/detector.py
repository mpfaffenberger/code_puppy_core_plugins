"""Pattern detection for git force push commands.

Detects force push patterns in shell commands, covering all the sneaky
ways git lets you wreck a remote branch.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass
class ForcePushMatch:
    """Result of a force push pattern match."""

    pattern_name: str
    description: str


# Each input has already been split at unquoted shell operators.
_GIT_PUSH_RE = re.compile(r"^\s*git\s+push\b")

# Ordered by specificity, first match wins.
# Each tuple: (compiled regex, human-readable name, what it catches)
_FORCE_PUSH_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (
        re.compile(r"\bgit\s+push\b.*--force-with-lease"),
        "--force-with-lease",
        "force push with lease (safer, but still rewrites history)",
    ),
    (
        re.compile(r"\bgit\s+push\b.*--force-if-includes"),
        "--force-if-includes",
        "force push with includes check (still rewrites history)",
    ),
    (
        re.compile(r"\bgit\s+push\b.*--force"),
        "--force",
        "force push (rewrites remote history)",
    ),
    (
        re.compile(r"\bgit\s+push\b.*\s-f\b"),
        "-f",
        "force push shorthand (rewrites remote history)",
    ),
    (
        re.compile(r"\bgit\s+push\b.*\s-F\b"),
        "-F",
        "force push shorthand (rewrites remote history)",
    ),
    # The +refspec syntax: git push origin +main, git push origin +HEAD:main
    (
        re.compile(r"\bgit\s+push\b.*\s\+"),
        "+refspec",
        "force push via +refspec prefix (rewrites remote history)",
    ),
]


def _is_git_push_a_command(command: str) -> bool:
    """Check that 'git push' is an actual command, not a string argument.

    Expects one command from the quote-aware boundary scan, avoiding
    false positives like "echo 'git push --force'".

    Args:
        command: The shell command string to inspect.

    Returns:
        True if 'git push' appears as an actual command invocation.
    """
    return bool(_GIT_PUSH_RE.search(command))


def _shell_commands(command: str) -> Iterator[str]:
    """Split at unquoted shell operators, preserving quotes and escapes.

    This is a lexical boundary scan, not a full shell interpreter. In
    particular, quoted arguments must not introduce command boundaries.
    """
    start = 0
    quote: str | None = None
    escaped = False
    comment = False
    for index, char in enumerate(command):
        if comment:
            if char == "\n":
                comment = False
                start = index + 1
            continue
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "#" and (index == start or command[index - 1].isspace()):
            yield command[start:index]
            comment = True
        elif char in ";&|\n()":
            yield command[start:index]
            start = index + 1
    if not comment:
        yield command[start:]


def detect_force_push(command: str) -> ForcePushMatch | None:
    """Check if a shell command contains a git force push.

    Args:
        command: The shell command string to inspect.

    Returns:
        ForcePushMatch if a force push pattern is found, None otherwise.
    """
    # Quick pre-filter: skip entirely if "push" isn't even in the command
    if "push" not in command:
        return None

    for shell_command in _shell_commands(command):
        # Flags belonging to a later command must not affect this push.
        if not _is_git_push_a_command(shell_command):
            continue
        for pattern, name, description in _FORCE_PUSH_PATTERNS:
            if pattern.search(shell_command):
                return ForcePushMatch(pattern_name=name, description=description)

    return None
