"""Launcher lifecycle, transport, and error-handling regression contracts."""

from unittest.mock import Mock

import pytest

from code_puppy_core_plugins.herdr import launcher
from tests.herdr_launcher_support import FakeClient, split_calls


def test_initial_prompt_is_one_child_argument_even_idle_before_editor(launcher_env):
    client = FakeClient()
    client.states = ["idle"]  # startup reports idle before editor enables paste
    launcher.spawn(client, "fixer", prompt="line one\nline two")
    runs = [c for c in client.calls if c[:2] == ("pane", "run")]
    assert len(runs) == 1
    assert (
        launcher.Path(launcher.shlex.split(runs[0][3])[4]).read_text()
        == "line one\nline two"
    )


@pytest.mark.parametrize(
    "failure",
    [UnicodeDecodeError("utf-8", b"\x90", 0, 1, "bad"), TypeError("bad agents")],
)
def test_any_post_split_failure_reports_pane(launcher_env, failure):
    class Broken(FakeClient):
        def call(self, *args):
            if args[:2] == ("pane", "run"):
                raise failure
            return super().call(*args)

    with pytest.raises(launcher.LauncherError, match="w1:p4"):
        launcher.spawn(Broken(), "fixer")


def test_launch_ignores_stripped_exe_relative_argv_and_python_flags(monkeypatch):
    monkeypatch.setattr(launcher.sys, "executable", "/same/python")
    monkeypatch.setattr(launcher.sys, "argv", ["missing/Scripts/pup", "-m", "model"])
    monkeypatch.setattr(
        launcher.sys,
        "orig_argv",
        ["/same/python", "-I", "-X", "utf8", "-m", "code_puppy"],
    )
    assert launcher.launch_argv(["--model", "child"]) == [
        "/same/python",
        "-P",
        "-m",
        "code_puppy",
        "--model",
        "child",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Don't forget the tests",
        "'it''s'",
        "a    b   c",
        "--verbose output please",
        "@alice asked for this",
    ],
)
def test_send_preserves_literal_tail(launcher_env, text):
    send = Mock(return_value="ok")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(launcher, "send", send)
        assert (
            launcher.execute("/herdr send fixer " + text, client_factory=lambda: None)
            == "ok"
        )
    assert send.call_args.args[2] == text


def test_send_explicit_file(launcher_env, monkeypatch):
    (launcher_env / "brief.txt").write_text("single line", encoding="utf-8")
    send = Mock(return_value="ok")
    monkeypatch.setattr(launcher, "send", send)
    assert (
        launcher.execute(
            "/herdr send fixer --file brief.txt", client_factory=lambda: None
        )
        == "ok"
    )
    assert send.call_args.args[2] == "single line"


def test_windows_option_paths_keep_backslashes():
    assert launcher._words(
        r'/herdr spawn fixer --cwd "C:\my project" --prompt-file C:\briefs\brief.md',
        windows=True,
    ) == [
        "/herdr",
        "spawn",
        "fixer",
        "--cwd",
        r"C:\my project",
        "--prompt-file",
        r"C:\briefs\brief.md",
    ]


def test_oversized_before_split(launcher_env):
    client = FakeClient()
    with pytest.raises(launcher.LauncherError, match="large"):
        launcher.spawn(client, "fixer", prompt="a" * 33000)
    assert not split_calls(client)


def test_missing_caller_geometry_is_actionable(launcher_env):
    client = FakeClient()
    original = client.call
    client.call = lambda *args: (
        {"layout": {"panes": []}} if args[:2] == ("pane", "layout") else original(*args)
    )
    with pytest.raises(launcher.LauncherError, match="caller pane"):
        launcher.spawn(client, "fixer")


def test_visual_square_splits_down(launcher_env):
    client = FakeClient()
    client.rect = {"width": 80, "height": 40}
    launcher.spawn(client, "fixer")
    assert split_calls(client)[0][5] == "down"


def test_utf8_cli(monkeypatch):
    run = Mock(return_value=Mock(returncode=0, stdout='{"result":{}}', stderr=""))
    monkeypatch.setattr(launcher.subprocess, "run", run)
    launcher.ControlClient().call("agent", "list")
    assert run.call_args.kwargs["encoding"] == "utf-8"


@pytest.mark.parametrize("prompt", ["/exit", "!rm file", "first\nsecond"])
def test_send_rejects_commands_and_multiline_before_input(launcher_env, prompt):
    client = FakeClient()
    with pytest.raises(launcher.LauncherError):
        launcher.send(client, "fixer", prompt)
    assert client.calls == []


def test_spawn_rejects_command_passthrough(launcher_env):
    with pytest.raises(launcher.LauncherError):
        launcher.spawn(FakeClient(), "fixer", prompt="!echo hi")


def test_help_and_bare_command(launcher_env):
    for command in ["/herdr", "/herdr help"]:
        assert "spawn" in launcher.execute(command) and "send" in launcher.execute(
            command
        )


def test_powershell_smart_quotes_are_escaped(monkeypatch):
    monkeypatch.setattr(launcher, "_windows", lambda: True)
    assert launcher._shell_command(["a\u2018b"]) == "& 'a\u2018\u2018b'"


def test_disappeared_child_fails_fast(launcher_env):
    class Gone(FakeClient):
        def __init__(self):
            super().__init__()
            self.polls = 0

        def call(self, *args):
            if args[:2] == ("agent", "get"):
                self.polls += 1
                if self.polls > 1:
                    raise launcher.LauncherError("gone", code="agent_not_found")
                return {
                    "agent": {
                        "agent": "codepuppy",
                        "agent_status": "working",
                        "pane_id": "w1:p4",
                    }
                }
            return super().call(*args)

    client = Gone()
    with pytest.raises(launcher.LauncherError, match="disappeared"):
        launcher.spawn(client, "fixer")
    assert client.polls == 2


def test_test_runner_disconnects_inherited_live_server(monkeypatch):
    from tests import conftest

    for name in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH"):
        monkeypatch.setenv(name, "inherited-live-context")
    conftest._isolate_herdr_environment()
    assert all(
        name not in launcher.os.environ
        for name in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH")
    )


def test_turn_end_keeps_agent_identity_shutdown_releases():
    from code_puppy_core_plugins.herdr.reporter import HerdrReporter
    from tests.herdr_test_support import FakeClient as ReporterClient

    client = ReporterClient()
    reporter = HerdrReporter(client)
    reporter.on_startup()
    reporter.on_run_start()
    reporter.on_turn_end()
    assert client.states[-1][0] == "idle"
    assert not client.closed
    reporter.on_shutdown()
    assert client.closed
