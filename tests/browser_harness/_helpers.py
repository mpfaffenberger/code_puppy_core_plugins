"""Shared fakes for the browser-harness plugin tests."""

from __future__ import annotations

import subprocess


class FakeAgent:
    """Stand-in for a pydantic-ai agent that captures ``@agent.tool`` calls."""

    def __init__(self) -> None:
        self.registered = {}

    def tool(self, function):
        self.registered[function.__name__] = function
        return function


class FakeSubprocess:
    """Replay one canned ``subprocess.run`` result and record the calls."""

    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        error: BaseException | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(
            argv, self.returncode, self.stdout, self.stderr
        )

    @property
    def env(self) -> dict:
        return self.calls[0][1]["env"]

    @property
    def stdin(self):
        return self.calls[0][1].get("stdin")

    @property
    def argv(self) -> list[str]:
        return self.calls[0][0]
