"""Fresh review regressions at the core preprocessing and terminal boundaries."""

import json
import os
from unittest.mock import Mock

import pytest

from code_puppy_core_plugins.herdr import launcher
from tests.herdr_launcher_support import FakeClient


@pytest.mark.parametrize(
    "text",
    [
        "exit",
        "QUIT",
        "clear",
        '"/exit"',
        "'/clear'",
        '"!echo hi"',
        'photo.png "/exit"',
        '"" /clear',
    ],
)
def test_send_rejects_effective_commands_before_requests(launcher_env, text):
    client = FakeClient()
    with pytest.raises(launcher.LauncherError, match="commands"):
        launcher.send(client, "fixer", text)
    assert not client.calls


@pytest.mark.parametrize("text", ["x" * 1000, "é" * 500, "x" * 2000])
def test_send_portable_canonical_limit_before_requests(launcher_env, text):
    client = FakeClient()
    with pytest.raises(launcher.LauncherError, match="1,000"):
        launcher.send(client, "fixer", text)
    assert not client.calls


@pytest.mark.parametrize(
    "body", [[], None, 1, "reply", {"error": None}, {"error": []}, {"error": "failed"}]
)
@pytest.mark.parametrize("returncode", [0, 1])
def test_cli_malformed_envelope_is_actionable(monkeypatch, body, returncode):
    encoded = json.dumps(body)
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        Mock(return_value=Mock(returncode=returncode, stdout=encoded, stderr=encoded)),
    )
    with pytest.raises(launcher.LauncherError, match="invalid response"):
        launcher.ControlClient().call("agent", "list")


@pytest.mark.skipif(os.name == "nt", reason="POSIX canonical PTY contract")
def test_maximum_send_fits_ready_canonical_reader(launcher_env):
    import select
    import termios

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
    text = "x" * 999
    launcher.send(client, "fixer", text)
    assert client.calls[-1][3] == text
    master, slave = os.openpty()
    try:
        settings = termios.tcgetattr(slave)
        settings[3] |= termios.ICANON
        settings[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, settings)
        # Include bracketed-paste markers, even if classic input treats them
        # as ordinary bytes rather than editor framing.
        wire = b"\x1b[200~" + text.encode() + b"\x1b[201~\n"
        assert os.write(master, wire) == len(wire)
        assert select.select([slave], [], [], 1)[0], "canonical Enter was lost"
        assert os.read(slave, 2048) == wire
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("state", ["working", "blocked", "unknown"])
def test_readiness_refuses_non_idle_puppy(state):
    assert not launcher._ready({"agent": "codepuppy", "agent_status": state})


def test_help_exposes_operational_options(launcher_env):
    usage = launcher.execute("/herdr help")
    for option in ("--direction", "--cwd", "--timeout", "30", "300"):
        assert option in usage


def test_menu_advertises_single_line():
    from code_puppy_core_plugins.herdr.register_callbacks import _launcher_help

    assert "single-line" in _launcher_help()[0][1]


def test_core_boundary_file_brief_avoids_quote_loss(launcher_env, monkeypatch):
    from code_puppy.command_line.attachments import parse_prompt_attachments
    from code_puppy_core_plugins.herdr.register_callbacks import _launcher_command

    (launcher_env / "brief.md").write_text("Review the tests\nincluding edge cases")
    spawn = Mock(return_value="ok")
    monkeypatch.setattr(launcher, "spawn", spawn)
    command = parse_prompt_attachments(
        "/herdr spawn fixer --prompt-file brief.md"
    ).prompt
    assert _launcher_command(command, "herdr") == "ok"
    assert spawn.call_args.kwargs["prompt"] == "Review the tests\nincluding edge cases"


def test_core_boundary_normalizes_inline_send(launcher_env, monkeypatch):
    from code_puppy.command_line.attachments import parse_prompt_attachments
    from code_puppy_core_plugins.herdr.register_callbacks import _launcher_command

    send = Mock(return_value="ok")
    monkeypatch.setattr(launcher, "send", send)
    command = parse_prompt_attachments('/herdr send fixer "a    b"').prompt
    assert _launcher_command(command, "herdr") == "ok"
    assert send.call_args.args[2] == "a b"
