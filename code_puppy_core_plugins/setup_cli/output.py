"""Plain-stdout output helpers for the setup CLI.

The setup CLI runs from ``handle_cli_args``, before Code Puppy's
messaging renderers start. ``emit_*`` messages queued there are never
rendered (verified by spike), so every setup command writes to real
stdout/stderr through this module instead.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable

SENSITIVE_MARKERS = ("key", "token", "secret", "password", "credential")


class SetupError(Exception):
    """A user-facing setup failure carrying a process exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class Out:
    """Result printer honoring the global ``--json`` flag."""

    def __init__(self, json_mode: bool = False) -> None:
        self.json_mode = json_mode

    def result(self, data: Any, human: str | Iterable[str] | None = None) -> None:
        if self.json_mode:
            print(json.dumps(data, indent=2, sort_keys=True, default=str))
            return
        if human is None:
            human = _render_data(data)
        if isinstance(human, str):
            print(human)
        else:
            for line in human:
                print(line)

    def info(self, message: str) -> None:
        if not self.json_mode:
            print(message)

    def warn(self, message: str) -> None:
        print(message if self.json_mode else f"warning: {message}", file=sys.stderr)


def _render_data(data: Any) -> list[str]:
    if isinstance(data, dict):
        return [f"{key}: {value}" for key, value in data.items()]
    if isinstance(data, (list, tuple)):
        return [str(item) for item in data]
    return [str(data)]


def mask_value(key: str, value: Any) -> str:
    """Render a config/credential value without ever exposing a secret."""
    if value is None or value == "":
        return "(unset)"
    lowered = key.lower()
    if any(marker in lowered for marker in SENSITIVE_MARKERS):
        return "(set, hidden)"
    return str(value)


def confirm(prompt: str, *, assume_yes: bool) -> bool:
    """Ask before a destructive/code-executing action; never blocks on --yes."""
    if assume_yes:
        return True
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")
