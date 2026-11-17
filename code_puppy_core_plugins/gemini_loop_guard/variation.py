"""Spot "the same tool with small variations", whatever the calls return.

The streak logic in ``policy`` asks "did this call teach the model anything
new?". A model digging in the wrong place keeps getting *something* back, so
that question never fires. This module asks a different one: are the last few
investigation calls near-identical to each other?

Only investigation tools are watched (search and shell). Reading files is
progress and mutating tools mean state changed, so a mutation clears the
history. Deliberate polling (``sleep``) is exempt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Tools whose near-identical repeats suggest spinning.
INVESTIGATION_TOOLS = frozenset({"grep", "agent_run_shell_command"})
# Tools that change state: re-running a check afterwards is legitimate.
MUTATING_TOOLS = frozenset(
    {"create_file", "replace_in_file", "delete_file", "delete_snippet", "edit_file"}
)

WINDOW = 8  # how many recent investigation calls a call is compared with
SIMILARITY = 0.6  # token Jaccard at or above which two calls are "the same"

_TOKEN = re.compile(r"[a-z0-9_]+")
_COMMAND_START = r"(?:^|(?<=[;&|\n]))\s*"
_CD_PREFIX = re.compile(_COMMAND_START + r"cd\s+\S+\s*(?:&&|;|\n|$)")
_ENV_ASSIGNMENT = re.compile(
    _COMMAND_START + r"(?:export\s+)?[A-Za-z_]\w*=(?:\"[^\"]*\"|'[^']*'|\S*)\s*"
)
_VENV_ACTIVATE = re.compile(r"\bsource\s+\S*activate\b")
_ABSOLUTE_PATH = re.compile(r"(?<![\w.])/(?:[\w.\-@+]+/)+([\w.\-@+]+)")
_SLEEP = re.compile(r"\bsleep\s+\d")
# A word that names a file: it has an extension. ``.venv/bin/python`` style
# tool paths are filtered out by requiring a letter-led extension of 1-5 chars.
_FILE_NAME = re.compile(r"[\w\-]+\.[a-z][a-z0-9]{0,4}\b")

_SUMMARY_CHARS = 70


@dataclass(frozen=True)
class CallShape:
    """What matters about a call when comparing it with other calls."""

    tokens: frozenset[str]
    files: frozenset[str]

    def resembles(self, other: CallShape) -> bool:
        # Two calls that name different files are looking at different things,
        # which is progress, just like reading another file.
        if self.files and other.files and self.files.isdisjoint(other.files):
            return False
        return _jaccard(self.tokens, other.tokens) >= SIMILARITY


def call_shape(tool_name: str, tool_args: Any) -> CallShape:
    """Normalise a call for comparison."""
    if not isinstance(tool_args, dict):
        return CallShape(frozenset(_TOKEN.findall(str(tool_args).lower())), frozenset())
    if tool_name == "agent_run_shell_command":
        text = _strip_shell_noise(str(tool_args.get("command", "")))
        files = frozenset(_FILE_NAME.findall(text.lower()))
    else:
        # Only the pattern: a shared directory says nothing about similarity.
        text, files = str(tool_args.get("search_string", "")), frozenset()
    return CallShape(frozenset(_TOKEN.findall(text.lower())), files)


def _strip_shell_noise(command: str) -> str:
    """Drop the parts of a command that differ without changing its purpose."""
    previous = None
    while previous != command:
        previous = command
        command = _CD_PREFIX.sub(" ", command, count=1)
        command = _ENV_ASSIGNMENT.sub(" ", command, count=1)
    command = _VENV_ACTIVATE.sub(" ", command)
    return _ABSOLUTE_PATH.sub(r"\1", command)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


@dataclass(frozen=True)
class _Entry:
    tool_name: str
    shape: CallShape
    summary: str


@dataclass
class VariationDetector:
    """Remembers recent investigation calls and fires once per run of look-alikes."""

    history: list[_Entry] = field(default_factory=list)
    armed: bool = True

    def observe(self, tool_name: str, tool_args: Any, fire_after: int) -> str | None:
        """Record a call; return a description of the look-alikes if it is the
        ``fire_after``-th near-identical call in a row (once per run of them)."""
        if tool_name in MUTATING_TOOLS:
            self.history.clear()
            self.armed = True
            return None
        if tool_name not in INVESTIGATION_TOOLS or not fire_after:
            return None
        if tool_name == "agent_run_shell_command" and _SLEEP.search(
            str(tool_args.get("command", "") if isinstance(tool_args, dict) else "")
        ):
            return None

        shape = call_shape(tool_name, tool_args)
        similar = [
            entry
            for entry in self.history[-(WINDOW - 1) :]
            if entry.tool_name == tool_name and entry.shape.resembles(shape)
        ]
        entry = _Entry(tool_name, shape, _summary(tool_name, tool_args))
        self.history.append(entry)

        if len(similar) + 1 < fire_after:
            self.armed = True
            return None
        if not self.armed:
            return None
        self.armed = False
        return _describe(tool_name, similar[-(fire_after - 1) :] + [entry])


def _summary(tool_name: str, tool_args: Any) -> str:
    if isinstance(tool_args, dict):
        raw = tool_args.get("command") or tool_args.get("search_string") or ""
    else:
        raw = tool_args
    text = " ".join(str(raw).split())
    return text if len(text) <= _SUMMARY_CHARS else text[: _SUMMARY_CHARS - 3] + "..."


def _describe(tool_name: str, entries: list[_Entry]) -> str:
    listed = "\n".join(f"  {n}. {entry.summary}" for n, entry in enumerate(entries, 1))
    return (
        f"Your last {len(entries)} {tool_name} calls were near-identical:\n{listed}\n"
        "Check they still move you toward the user's question. If you already "
        "have enough to answer it, answer now; a partial answer is fine."
    )
