"""Explicit command intent: offline discovery and fake terminal writes only."""

from unittest.mock import Mock

import pytest

from code_puppy_core_plugins.herdr import launcher
from tests.herdr_launcher_support import FakeClient


def ready_client():
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
    return client


@pytest.fixture
def candidates(monkeypatch):
    from code_puppy.command_line import command_registry
    from code_puppy import callbacks

    handler = Mock(side_effect=AssertionError("Must never execute a handler"))
    monkeypatch.setattr(
        command_registry,
        "get_all_commands",
        lambda: {
            "model": Mock(handler=handler),
            "new": Mock(handler=handler),
            "n": Mock(handler=handler),
            "help": Mock(handler=handler),
        },
    )
    monkeypatch.setattr(
        callbacks,
        "on_custom_command_help",
        lambda: [
            [("flux/status", "Status"), ("custom", "Custom")],
            ("legacy", "Legacy"),
            ["/old - Old help"],
            None,
        ],
    )
    return handler


@pytest.mark.parametrize(
    "text",
    ["/model gpt-5", "/n", "/flux/status", "/custom /tmp/file", "/legacy", "/old"],
)
def test_command_types_exact_payload(launcher_env, candidates, text):
    client = ready_client()
    result = launcher.command(client, "fixer", text)
    assert client.calls[-1] == ("pane", "run", "w1:p4", text)
    assert "not confirmed executed" in result
    candidates.assert_not_called()


@pytest.mark.parametrize(
    "text",
    [
        "/unknown",
        "/models",
        "/",
        "//model",
        " /model",
        '"/model"',
        r"\ /model",
        "exit",
        "!pwd",
        "/model\n/new",
        "/model\tfoo",
        "/model\rfoo",
        "/model\x1bfoo",
        "/model\x85foo",
        '/model "unterminated',
        "/model " + "é" * 500,
    ],
)
def test_command_fails_closed_before_requests(launcher_env, candidates, text):
    client = ready_client()
    with pytest.raises(launcher.LauncherError):
        launcher.command(client, "fixer", text)
    assert not client.calls
    candidates.assert_not_called()


@pytest.mark.parametrize(
    "advertised", [("/flux/status", "Status"), [("/flux/status", "Status")]]
)
def test_slash_prefixed_plugin_help_is_discoverable(
    monkeypatch, candidates, advertised
):
    from code_puppy import callbacks
    from code_puppy_core_plugins.herdr.command_intent import (
        candidate_help,
        validate_command,
    )

    monkeypatch.setattr(callbacks, "on_custom_command_help", lambda: [advertised])
    assert "/flux/status" in candidate_help().split()
    assert validate_command("/flux/status") == "/flux/status"
    candidates.assert_not_called()


def test_legacy_help_whitespace_matches_core(monkeypatch, candidates):
    from code_puppy import callbacks
    from code_puppy_core_plugins.herdr.command_intent import (
        candidate_help,
        validate_command,
    )

    monkeypatch.setattr(
        callbacks, "on_custom_command_help", lambda: [["/widget  - Description"]]
    )
    assert "/widget" in candidate_help().split()
    assert validate_command("/widget") == "/widget"
    candidates.assert_not_called()


def test_empty_command_payload_has_missing_payload_diagnostic(launcher_env, candidates):
    client = ready_client()
    result = launcher.execute("/herdr command fixer ", client_factory=lambda: client)
    assert result == "herdr error: Command payload is missing."
    assert not client.calls
    candidates.assert_not_called()


@pytest.mark.parametrize("junk", ["--mutating", "please list only mutating ones"])
def test_discovery_rejects_trailing_arguments(launcher_env, monkeypatch, junk):
    from code_puppy_core_plugins.herdr import command_intent

    discovery = Mock(side_effect=AssertionError("No discovery for invalid input"))
    monkeypatch.setattr(command_intent, "candidate_help", discovery)
    result = launcher.execute(
        f"/herdr commands {junk}", client_factory=lambda: pytest.fail("No transport")
    )
    assert result == "herdr error: " + launcher._USAGE
    discovery.assert_not_called()


def test_execute_and_discovery(launcher_env, candidates):
    client = ready_client()
    result = launcher.execute(
        command="/herdr command fixer /model gpt-5", client_factory=lambda: client
    )
    assert "not confirmed executed" in result
    listing = launcher.execute(
        "/herdr commands", client_factory=lambda: pytest.fail("No transport")
    )
    for name in ("/model", "/n", "/flux/status", "/old"):
        assert name in listing
    assert "sender" in listing.lower() and "target" in listing


def test_agent_guidance_and_help(launcher_env, candidates):
    from code_puppy_core_plugins.herdr import register_callbacks as hooks

    guidance = hooks._launcher_prompt()
    for text in (
        "/herdr send",
        "/herdr command",
        "/herdr commands",
        "mutating",
        "editor",
        "target",
        "activate_skill('herdr-code-puppy')",
        "intentionally authorized",
        "not target",
        "No automatic retries",
        "not byte-preserved",
        "disabled/unavailable",
    ):
        assert text in guidance
    assert "command" in hooks._launcher_help()[0][1]
    assert "/herdr command" in launcher.execute("/herdr help")


def test_core_preprocessing_is_not_byte_preserving(launcher_env, candidates):
    from code_puppy.command_line.attachments import parse_prompt_attachments

    client = ready_client()
    raw = '/herdr command fixer /model "gpt-5"'
    processed = parse_prompt_attachments(raw).prompt
    launcher.execute(processed, client_factory=lambda: client)
    assert client.calls[-1][3] == "/model gpt-5"
    assert parse_prompt_attachments(client.calls[-1][3]).prompt == "/model gpt-5"


def test_attachment_cannot_promote_a_command(launcher_env, candidates):
    client = ready_client()
    with pytest.raises(launcher.LauncherError):
        launcher.command(client, "fixer", "brief.pdf /new")
    assert not client.calls


@pytest.mark.parametrize("state", ["working", "blocked", "unknown"])
def test_command_retains_idle_guard(launcher_env, candidates, state):
    client = ready_client()
    client.agents[0]["agent_status"] = state
    with pytest.raises(launcher.LauncherError):
        launcher.command(client, "fixer", "/new")
    assert not any(call[:2] == ("pane", "run") for call in client.calls)


def test_real_registry_alias_and_help_discovery(monkeypatch):
    from code_puppy import callbacks
    from code_puppy.command_line import command_handler, command_registry
    from code_puppy_core_plugins.herdr.command_intent import command_candidates

    monkeypatch.setattr(command_handler, "_ensure_plugins_loaded", lambda: None)
    monkeypatch.setattr(
        command_registry, "_ensure_plugin_commands_loaded", lambda: None
    )
    monkeypatch.setattr(command_registry, "_COMMAND_REGISTRY", {})
    handler = Mock(side_effect=AssertionError("No handler probes"))
    command_registry.register_command("example", "Example", aliases=["ex"])(handler)
    monkeypatch.setattr(
        callbacks, "on_custom_command_help", lambda: [[("ns/action", "Action")]]
    )
    builtin, custom = command_candidates()
    assert builtin == {"/example", "/ex"}
    assert custom == {"/ns/action"}
    handler.assert_not_called()


@pytest.mark.parametrize(
    "text", ['/model "a    b"', r"/model a\ b", "/model /tmp/file /new"]
)
def test_typed_arguments_versus_actual_child_preprocessing(
    launcher_env, candidates, text
):
    from code_puppy.command_line.attachments import parse_prompt_attachments

    client = ready_client()
    launcher.command(client, "fixer", text)
    assert client.calls[-1][3] == text
    assert parse_prompt_attachments(text).prompt.split()[0] == "/model"


def test_attachment_removal_keeps_authorized_leading_token(launcher_env, candidates):
    from code_puppy.command_line.attachments import parse_prompt_attachments

    attachment = launcher_env / "clip.mp4"
    attachment.write_bytes(b"fake video; parser only, never a handler")
    raw = f"/model {attachment} /new"
    client = ready_client()
    launcher.command(client, "fixer", raw)
    assert client.calls[-1][3] == raw
    processed = parse_prompt_attachments(raw).prompt
    assert processed == "/model /new"


def test_command_identity_recheck_and_no_retries(launcher_env, candidates):
    client = ready_client()
    client.states = ["working"]
    with pytest.raises(launcher.LauncherError, match="Do not blindly retry"):
        launcher.command(client, "fixer", "/new")
    assert not any(call[:2] == ("pane", "run") for call in client.calls)
    client = ready_client()
    client.fail_run = True
    with pytest.raises(launcher.LauncherError, match="Do not blindly retry"):
        launcher.command(client, "fixer", "/new")
    assert sum(call[:2] == ("pane", "run") for call in client.calls) == 1


def test_command_outside_herdr_and_prompt_hook_inactive(monkeypatch, candidates):
    from code_puppy_core_plugins.herdr import register_callbacks as hooks

    monkeypatch.delenv("HERDR_ENV", raising=False)
    client = ready_client()
    with pytest.raises(launcher.LauncherError, match="inside herdr"):
        launcher.command(client, "fixer", "/new")
    assert not client.calls
    assert hooks._launcher_prompt() is None


def test_real_builtin_discovery_never_runs_handler(launcher_env, monkeypatch):
    from code_puppy import callbacks
    from code_puppy.command_line import command_registry
    from code_puppy_core_plugins.herdr.command_intent import command_candidates

    monkeypatch.setattr(
        command_registry, "_ensure_plugin_commands_loaded", lambda: None
    )
    monkeypatch.setattr(callbacks, "on_custom_command_help", lambda: [])
    builtin, _ = command_candidates()
    assert {"/model", "/exit", "/quit"} <= builtin
    info = command_registry.get_command("model")
    handler = Mock(side_effect=AssertionError("Never run /model in tests"))
    monkeypatch.setattr(info, "handler", handler)
    client = ready_client()
    launcher.command(client, "fixer", "/model gpt-5")
    handler.assert_not_called()


@pytest.mark.parametrize("windows", [False, True])
def test_platform_argument_paths_preserved(
    launcher_env, candidates, monkeypatch, windows
):
    from code_puppy_core_plugins.herdr import command_intent

    monkeypatch.setattr(command_intent, "os", Mock(name="nt" if windows else "posix"))
    # os.name is a string, not the Mock's own diagnostic name.
    command_intent.os.name = "nt" if windows else "posix"
    text = r'/model "C:\path with spaces\file" /tmp/file'
    client = ready_client()
    launcher.command(client, "fixer", text)
    assert client.calls[-1][3] == text


def test_namespaced_registry_without_custom_help_is_not_dispatchable(
    launcher_env, monkeypatch
):
    from code_puppy.command_line import command_registry
    from code_puppy import callbacks

    monkeypatch.setattr(
        command_registry, "get_all_commands", lambda: {"ns/action": Mock()}
    )
    monkeypatch.setattr(callbacks, "on_custom_command_help", lambda: [])
    client = ready_client()
    with pytest.raises(launcher.LauncherError, match="Unknown"):
        launcher.command(client, "fixer", "/ns/action")
    assert not client.calls
