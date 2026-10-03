"""Shared fake control client and public pytest fixture for launchers."""

import pytest

from code_puppy_core_plugins.herdr import launcher


class FakeClient:
    def __init__(self):
        self.calls = []
        self.agents = []
        self.states = ["unknown", "working", "idle"]
        self.rect = {"width": 120, "height": 30}
        self.fail_run = False

    def call(self, *args):
        self.calls.append(args)
        if args == ("agent", "list"):
            agents = self.agents
            if any(c[:2] == ("pane", "run") for c in self.calls):
                agents = [*agents, {"pane_id": "w1:p4", "agent": "codepuppy"}]
            return {"agents": agents}
        if args[:2] == ("pane", "layout"):
            return {"layout": {"panes": [{"pane_id": "w1:p3", "rect": self.rect}]}}
        if args[:2] == ("pane", "split"):
            return {"pane": {"pane_id": "w1:p4"}}
        if args[:2] == ("pane", "run") and self.fail_run:
            raise launcher.LauncherError("launch failed")
        if args[:2] == ("agent", "get"):
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {
                "agent": {
                    "pane_id": "w1:p4",
                    "name": "fixer",
                    "agent": "codepuppy",
                    "agent_status": state,
                    "terminal_id": "term-child",
                }
            }
        return {}


@pytest.fixture
def launcher_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p3")
    monkeypatch.chdir(tmp_path)
    from code_puppy_core_plugins.herdr import launch_command

    monkeypatch.setattr(launch_command.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(
        launcher,
        "launch_argv",
        lambda args: ["/my venv/bin/python", "/my venv/bin/pup", *args],
    )
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    return tmp_path


def split_calls(client):
    return [call for call in client.calls if call[:2] == ("pane", "split")]
