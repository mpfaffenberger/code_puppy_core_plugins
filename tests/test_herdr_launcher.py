"""Launcher contracts: fake control client, no live server."""

from unittest.mock import Mock

import pytest

from code_puppy_core_plugins.herdr import launcher


from tests.herdr_launcher_support import FakeClient, split_calls


def test_outside_has_no_client_process_or_file_read(monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    factory = Mock(side_effect=AssertionError("must not connect"))
    result = launcher.execute(
        "/herdr spawn fixer --prompt-file missing", client_factory=factory
    )
    assert "inside herdr" in result
    factory.assert_not_called()


@pytest.mark.parametrize(
    "name", ["Bad", "a.b", "a;touch", "1bad", "a" * 33, "bad\nname"]
)
def test_invalid_name_before_split(launcher_env, name):
    client = FakeClient()
    with pytest.raises(launcher.LauncherError, match="name"):
        launcher.spawn(client, name)
    assert client.calls == []


def test_duplicate_before_split(launcher_env):
    client = FakeClient()
    client.agents = [{"name": "fixer", "pane_id": "w2:p1"}]
    with pytest.raises(launcher.LauncherError, match="already"):
        launcher.spawn(client, "fixer")
    assert not split_calls(client)


@pytest.mark.parametrize(
    "rect,direction",
    [({"width": 120, "height": 30}, "right"), ({"width": 20, "height": 40}, "down")],
)
def test_spawn_preserves_cwd_focus_waits_then_one_multiline_submission(
    launcher_env, rect, direction
):
    client = FakeClient()
    client.rect = rect
    result = launcher.spawn(
        client, "fixer", prompt="first\nsecond", args=["--model", "some model"]
    )
    split = split_calls(client)[0]
    assert split == (
        "pane",
        "split",
        "--pane",
        "w1:p3",
        "--direction",
        direction,
        "--cwd",
        str(launcher_env),
        "--no-focus",
    )
    launch = next(call for call in client.calls if call[:2] == ("pane", "run"))
    assert launch[2] == "w1:p4"
    argv = launcher.shlex.split(launch[3])
    assert argv[:3] == ["/my venv/bin/python", "-P", "-c"]
    assert argv[-2:] == ["--model", "some model"]
    assert launcher.Path(argv[4]).read_text() == "first\nsecond"
    assert client.calls.index(
        ("agent", "rename", "w1:p4", "fixer")
    ) > client.calls.index(launch)
    assert len([c for c in client.calls if c[:2] == ("agent", "get")]) == 1
    assert len([c for c in client.calls if c[:2] == ("pane", "run")]) == 1
    assert "w1:p4" in result and "fixer" in result


@pytest.mark.parametrize("failure", ["run", "timeout"])
def test_failure_reports_created_pane_without_closing(launcher_env, failure):
    client = FakeClient()
    client.fail_run = failure == "run"
    client.states = ["working"]
    with pytest.raises(launcher.LauncherError, match="w1:p4"):
        launcher.spawn(client, "fixer", timeout=0.001)
    assert not any(c[:2] == ("pane", "close") for c in client.calls)
    assert len([c for c in client.calls if c[:2] == ("pane", "run")]) == 1


def test_prompt_file_and_args(launcher_env):
    (launcher_env / "brief.md").write_text("first\nsecond", encoding="utf-8")
    client = FakeClient()
    result = launcher.execute(
        '/herdr spawn fixer --direction down --prompt-file brief.md -- --model "some model"',
        client_factory=lambda: client,
    )
    assert "w1:p4" in result
    runs = [c for c in client.calls if c[:2] == ("pane", "run")]
    assert len(runs) == 1
    assert (
        launcher.Path(launcher.shlex.split(runs[0][3])[4]).read_text()
        == "first\nsecond"
    )


@pytest.mark.parametrize(
    "options",
    [
        "--prompt-file absent",
        "--prompt one --prompt-file absent",
        "--direction left",
        "--timeout 0",
        "--timeout nan",
    ],
)
def test_bad_options_before_split(launcher_env, options):
    client = FakeClient()
    result = launcher.execute(
        f"/herdr spawn fixer {options}", client_factory=lambda: client
    )
    assert "error" in result.lower()
    assert not split_calls(client)


def test_send_requires_unique_idle_puppy_and_submits_once(launcher_env):
    client = FakeClient()
    client.states = ["idle"]
    client.agents = [
        {
            "name": "fixer",
            "pane_id": "w1:p4",
            "agent": "codepuppy",
            "agent_status": "idle",
            "terminal_id": "term-child",
        }
    ]
    result = launcher.execute(
        "/herdr send fixer first second", client_factory=lambda: client
    )
    assert "w1:p4" in result
    assert client.calls[-1] == ("pane", "run", "w1:p4", "first second")


@pytest.mark.parametrize(
    "kind,state",
    [("claude", "idle"), ("codepuppy", "blocked"), ("codepuppy", "working")],
)
def test_send_rejects_wrong_kind_or_state(launcher_env, kind, state):
    client = FakeClient()
    client.agents = [
        {"name": "fixer", "pane_id": "w1:p4", "agent": kind, "agent_status": state}
    ]
    result = launcher.execute("/herdr send fixer hello", client_factory=lambda: client)
    assert "error" in result.lower()
    assert not any(c[:2] == ("pane", "run") for c in client.calls)


@pytest.mark.parametrize("prompt", ["", "hello\x1b[201~\rquit", "bad\x00text"])
def test_unsafe_prompt_before_split(launcher_env, prompt):
    client = FakeClient()
    with pytest.raises(launcher.LauncherError):
        launcher.spawn(client, "fixer", prompt=prompt)
    assert not split_calls(client)


def test_cli_run_success_has_no_stdout(monkeypatch):
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        Mock(return_value=Mock(returncode=0, stdout="", stderr="")),
    )
    assert launcher.ControlClient().call("pane", "run", "w1:p4", "hello") == {}


def test_cli_empty_read_is_not_success(monkeypatch):
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        Mock(return_value=Mock(returncode=0, stdout="", stderr="")),
    )
    with pytest.raises(launcher.LauncherError):
        launcher.ControlClient().call("agent", "list")


def test_launch_uses_current_interpreter_entrypoint_not_path(monkeypatch, tmp_path):
    entry = tmp_path / "pup"
    entry.write_text("# entry point")
    monkeypatch.setattr(launcher.sys, "executable", "/same/python")
    monkeypatch.setattr(launcher.sys, "argv", [str(entry), "--do-not-inherit"])
    monkeypatch.setattr(launcher.sys, "orig_argv", ["/same/python", str(entry)])
    assert launcher.launch_argv(["--model", "test"]) == [
        "/same/python",
        "-P",
        "-m",
        "code_puppy",
        "--model",
        "test",
    ]


def test_launch_preserves_module_invocation(monkeypatch):
    monkeypatch.setattr(launcher.sys, "executable", "/same/python")
    monkeypatch.setattr(
        launcher.sys, "orig_argv", ["/same/python", "-m", "code_puppy", "old args"]
    )
    assert launcher.launch_argv([]) == ["/same/python", "-P", "-m", "code_puppy"]


def test_cli_failure_does_not_retry(monkeypatch):
    run = Mock(
        return_value=Mock(
            returncode=1, stdout="", stderr='{"error":{"message":"rejected"}}'
        )
    )
    monkeypatch.setattr(launcher.subprocess, "run", run)
    with pytest.raises(launcher.LauncherError, match="rejected"):
        launcher.ControlClient().call("pane", "run", "w1:p4", "hello")
    assert run.call_count == 1


def test_child_not_yet_reported_keeps_waiting(launcher_env):
    class SlowClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.polls = 0

        def call(self, *args):
            result = super().call(*args)
            if args[:2] == ("agent", "get"):
                self.polls += 1
                if self.polls < 4:
                    raise launcher.LauncherError("not yet", code="agent_not_found")
            return result

    client = SlowClient()
    assert "w1:p4" in launcher.spawn(client, "fixer")
    assert client.polls >= 4


def test_send_rejects_replaced_terminal(launcher_env):
    client = FakeClient()
    client.states = ["idle"]
    client.agents = [
        {
            "name": "fixer",
            "pane_id": "w1:p4",
            "agent": "codepuppy",
            "agent_status": "idle",
            "terminal_id": "other",
        }
    ]
    assert "nothing sent" in launcher.execute(
        "/herdr send fixer hello", client_factory=lambda: client
    )
    assert not any(c[:2] == ("pane", "run") for c in client.calls)


def test_send_file(launcher_env):
    (launcher_env / "followup.md").write_text("one two", encoding="utf-8")
    client = FakeClient()
    client.states = ["idle"]
    client.agents = [
        {
            "name": "fixer",
            "pane_id": "w1:p4",
            "agent": "codepuppy",
            "agent_status": "idle",
            "terminal_id": "term-child",
        }
    ]
    assert "submitted" in launcher.execute(
        "/herdr send fixer --file followup.md", client_factory=lambda: client
    )
    assert client.calls[-1] == ("pane", "run", "w1:p4", "one two")


def test_custom_callback_routes_only_herdr(monkeypatch):
    from code_puppy_core_plugins.herdr import register_callbacks as callbacks

    execute = Mock(return_value="handled")
    monkeypatch.setattr(launcher, "execute", execute)
    assert callbacks._launcher_command("/other", "other") is None
    execute.assert_not_called()
    assert callbacks._launcher_command("/herdr spawn fixer", "herdr") == "handled"
    execute.assert_called_once_with("/herdr spawn fixer")
    assert callbacks._launcher_help()[0][0] == "herdr"


def test_console_script_model_flag_is_not_python_module(monkeypatch, tmp_path):
    entry = tmp_path / "pup"
    entry.write_text("# entry point")
    monkeypatch.setattr(launcher.sys, "executable", "/same/python")
    monkeypatch.setattr(launcher.sys, "argv", [str(entry), "-m", "model-name"])
    monkeypatch.setattr(
        launcher.sys, "orig_argv", ["/same/python", str(entry), "-m", "model-name"]
    )
    assert launcher.launch_argv(["--model", "child-model"]) == [
        "/same/python",
        "-P",
        "-m",
        "code_puppy",
        "--model",
        "child-model",
    ]
