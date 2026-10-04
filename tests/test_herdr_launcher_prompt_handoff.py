"""Initial prompt handoff and command-input regressions, without live panes."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from code_puppy_core_plugins.herdr import launcher
from tests.herdr_launcher_support import FakeClient


def test_prompted_working_child_is_named_immediately(launcher_env):
    client = FakeClient()
    client.states = ["working"]
    result = launcher.spawn(client, "fixer", prompt="long task", timeout=0.01)
    assert ("agent", "rename", "w1:p4", "fixer") in client.calls
    assert "initial command" in result
    assert len([c for c in client.calls if c[:2] == ("agent", "get")]) == 1


def test_long_line_uses_private_file_short_single_line_command(launcher_env):
    client = FakeClient()
    prompt = "x" * 2000 + "\nsecond line"
    launcher.spawn(client, "fixer", prompt=prompt)
    command = next(c[3] for c in client.calls if c[:2] == ("pane", "run"))
    assert len(command.encode()) < 1024 and "\n" not in command
    argv = launcher.shlex.split(command)
    path = Path(argv[argv.index("-c") + 2])
    assert path.read_text() == prompt
    assert path.stat().st_mode & 0o777 == 0o600
    path.unlink()


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_file_send_trailing_newline(launcher_env, monkeypatch, ending):
    (launcher_env / "follow.txt").write_bytes(("run linter" + ending).encode())
    send = Mock(return_value="ok")
    monkeypatch.setattr(launcher, "send", send)
    assert (
        launcher.execute(
            "/herdr send fixer --file follow.txt", client_factory=lambda: None
        )
        == "ok"
    )
    assert send.call_args.args[2] == "run linter"


def test_spawn_slash_text_is_not_a_command(launcher_env):
    assert "Started" in launcher.spawn(
        FakeClient(), "fixer", prompt="/Users/me/repo has a bug"
    )


def test_parser_missing_file_and_unknown_action(launcher_env, monkeypatch):
    send = Mock()
    monkeypatch.setattr(launcher, "send", send)
    assert "--file PATH" in launcher.execute("/herdr send fixer --file")
    send.assert_not_called()
    assert "send" in launcher.execute("/herdr bogus x")


def test_bootstrap_reads_once_deletes_and_forwards_one_argument(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from code_puppy_core_plugins.herdr.launch_command import BOOTSTRAP, prompt_file

    prompt = "x" * 2000 + "\nsecond line"
    handoff = prompt_file(prompt)
    package = tmp_path / "code_puppy"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(
        "import json,sys;print(json.dumps(sys.argv[1:]))"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", BOOTSTRAP, str(handoff), "--model", "test"],
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("HERDR_")},
            "PYTHONPATH": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == ["--model", "test", "--", prompt]
    assert not handoff.exists()


def test_duplicate_name_does_not_write_private_file(launcher_env, monkeypatch):
    client = FakeClient()
    client.agents = [{"name": "fixer"}]
    create = Mock(side_effect=AssertionError("no handoff before preflight"))
    monkeypatch.setattr(launcher, "prompt_file", create)
    with pytest.raises(launcher.LauncherError, match="already"):
        launcher.spawn(client, "fixer", prompt="hello")
    create.assert_not_called()


def test_failed_launch_cleans_private_file(launcher_env, monkeypatch):
    handoff = launcher_env / "private.txt"
    handoff.write_text("hello")
    monkeypatch.setattr(launcher, "prompt_file", lambda _: handoff)
    client = FakeClient()
    client.fail_run = True
    with pytest.raises(launcher.LauncherError, match="w1:p4"):
        launcher.spawn(client, "fixer", prompt="hello")
    assert not handoff.exists()


def test_oversized_child_args_fail_before_split(launcher_env):
    client = FakeClient()
    with pytest.raises(launcher.LauncherError, match="large"):
        launcher.spawn(client, "fixer", args=["x" * 1100])
    assert not any(c[:2] == ("pane", "split") for c in client.calls)
