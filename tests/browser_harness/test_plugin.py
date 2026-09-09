"""Consent gating, model tools, and the /browser slash command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_ai import BinaryContent, ToolReturn

from code_puppy_core_plugins.browser_harness import browser
from code_puppy_core_plugins.browser_harness import cli, commands, tools
from code_puppy_core_plugins.browser_harness import register_callbacks as rc

from ._helpers import FakeAgent, FakeSubprocess

EMIT = "code_puppy_core_plugins.browser_harness.commands.emit_"
RC_EMIT = "code_puppy_core_plugins.browser_harness.register_callbacks.emit_info"


def _registered(register, name):
    agent = FakeAgent()
    register(agent)
    return agent.registered[name]


def _installed(monkeypatch, fake=None):
    monkeypatch.setattr(cli, "executable", lambda: "/tools/browser-harness")
    monkeypatch.setattr(cli.subprocess, "run", fake or FakeSubprocess())
    return fake


# ── registration gating ─────────────────────────────────────────


def test_plugin_is_inert_until_the_user_opts_in(store):
    assert rc._register_tools() == []
    assert rc._register_agent_tools("main") == []
    assert rc._register_skills() == []
    assert rc._load_prompt() is None

    store.set_enabled(True)
    assert {item["name"] for item in rc._register_tools()} == set(tools.REGISTRARS)
    assert set(rc._register_agent_tools()) == set(tools.REGISTRARS)
    assert all(callable(item["register_func"]) for item in rc._register_tools())

    skills = rc._register_skills()
    assert [skill["name"] for skill in skills] == ["browser-harness"]
    assert Path(skills[0]["skill_md_path"]).is_file()
    assert "Firefox" in rc._load_prompt()


def test_startup_nudges_only_once_and_only_when_usable(store, monkeypatch):
    monkeypatch.setattr(cli, "installed", lambda: False)
    with patch(RC_EMIT) as info:
        rc._startup()
    assert info.call_args_list == []

    monkeypatch.setattr(cli, "installed", lambda: True)
    with patch(RC_EMIT) as info:
        rc._startup()
    assert "/browser enable" in info.call_args[0][0]

    store.set_enabled(True)
    with patch(RC_EMIT) as info:
        rc._startup()
    assert info.call_args_list == []


def test_a_broken_install_never_breaks_startup(monkeypatch):
    monkeypatch.setattr(
        cli, "installed", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with patch(RC_EMIT) as info:
        rc._startup()
    assert info.call_args_list == []


# ── tools ───────────────────────────────────────────────────────

TOOLS = "code_puppy_core_plugins.browser_harness.tools."


async def test_script_tool_returns_helper_stdout(store, monkeypatch):
    store.set_enabled(True)
    fake = _installed(monkeypatch, FakeSubprocess(stdout="Example Domain\n"))
    tool = _registered(tools.register_browser_harness, "browser_harness")

    result = await tool(None, "print(page_info()['title'])")

    assert result == {"success": True, "output": "Example Domain"}
    assert fake.calls[0][1]["input"] == "print(page_info()['title'])"


async def test_script_tool_refuses_without_consent(store, monkeypatch):
    _installed(monkeypatch)
    tool = _registered(tools.register_browser_harness, "browser_harness")

    result = await tool(None, "print(page_info())")

    assert result["success"] is False
    assert "/browser enable" in result["error"]


async def test_script_tool_attaches_the_documented_fix(store, monkeypatch):
    store.set_enabled(True)
    _installed(
        monkeypatch,
        FakeSubprocess(
            returncode=1,
            stderr="RuntimeError: permission-blocked: Chrome is reachable",
        ),
    )
    tool = _registered(tools.register_browser_harness, "browser_harness")

    result = await tool(None, "print(page_info())")

    assert result["success"] is False
    assert "mac-approve" in result["error"]


async def test_script_tool_teaches_the_install_when_it_is_missing(store, monkeypatch):
    store.set_enabled(True)
    monkeypatch.setattr(cli, "executable", lambda: None)
    tool = _registered(tools.register_browser_harness, "browser_harness")

    assert "uv tool install" in (await tool(None, "print(1)"))["error"]


async def test_script_tool_clamps_a_silly_timeout(store, monkeypatch):
    store.set_enabled(True)
    fake = _installed(monkeypatch, FakeSubprocess(stdout="ok"))
    tool = _registered(tools.register_browser_harness, "browser_harness")

    await tool(None, "print(1)", None, 1_000_000)

    assert fake.calls[0][1]["timeout"] == tools.MAX_TIMEOUT_SECONDS


async def test_screenshot_tool_shows_the_png(store, monkeypatch, tmp_path):
    store.set_enabled(True)
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    fake = _installed(monkeypatch, FakeSubprocess(stdout=f" {png} \n"))
    monkeypatch.setattr(tools, "emit_inline_image", lambda path: True)
    tool = _registered(tools.register_browser_screenshot, "browser_screenshot")

    result = await tool(None, True, None)

    assert isinstance(result, ToolReturn)
    assert result.metadata["path"] == str(png)
    assert result.metadata["displayed_inline"] is True
    assert any(isinstance(part, BinaryContent) for part in result.content)
    assert "full=True" in fake.calls[0][1]["input"]
    assert "max_dim=None" in fake.calls[0][1]["input"]


async def test_screenshot_tool_defaults_to_a_resized_viewport(
    store, monkeypatch, tmp_path
):
    store.set_enabled(True)
    png = tmp_path / "shot.png"
    png.write_bytes(b"png")
    fake = _installed(monkeypatch, FakeSubprocess(stdout=f"{png}\n"))
    tool = _registered(tools.register_browser_screenshot, "browser_screenshot")

    result = await tool(None, False, tools.DEFAULT_SCREENSHOT_MAX_DIM)

    assert f"max_dim={tools.DEFAULT_SCREENSHOT_MAX_DIM}" in fake.calls[0][1]["input"]
    assert result.metadata["displayed_inline"] is False


async def test_screenshot_tool_reports_a_missing_file(store, monkeypatch):
    store.set_enabled(True)
    _installed(monkeypatch, FakeSubprocess(stdout="/nowhere/shot.png\n"))
    tool = _registered(tools.register_browser_screenshot, "browser_screenshot")

    result = await tool(None, False, None)

    assert "no screenshot file" in result["error"]


async def test_doctor_tool_reports_health(store, monkeypatch):
    store.set_enabled(True)
    fake = _installed(monkeypatch, FakeSubprocess(stdout="chrome running   PASS\n"))
    tool = _registered(tools.register_browser_doctor, "browser_doctor")

    result = await tool(None)

    assert result["healthy"] is True
    assert fake.argv[-1] == "--doctor"


async def test_doctor_tool_flags_an_unhealthy_connection(store, monkeypatch):
    store.set_enabled(True)
    _installed(
        monkeypatch,
        FakeSubprocess(
            returncode=1,
            stdout="daemon alive   FAIL",
            stderr="permission-blocked: awaiting the Allow remote debugging popup",
        ),
    )
    tool = _registered(tools.register_browser_doctor, "browser_doctor")

    result = await tool(None)

    assert result["healthy"] is False
    assert "daemon alive" in result["report"]
    assert "mac-approve" in result["fix"]


# ── /browser ────────────────────────────────────────────────────


def test_command_help_advertises_browser():
    assert [name for name, _ in commands.command_help()] == ["browser"]


def test_other_slash_commands_pass_through():
    assert commands.handle_command("/computer-use status", "computer-use") is None


def test_enable_and_disable_persist(store):
    with patch(EMIT + "success"):
        assert commands.handle_command("/browser enable", "browser") is True
    assert store.is_enabled() is True

    with patch(EMIT + "warning"):
        commands.handle_command("/browser disable", "browser")
    assert store.is_enabled() is False


def test_enable_registers_the_tools_without_a_restart(store):
    with (
        patch("code_puppy.tools.get_available_tool_names") as refresh,
        patch(EMIT + "success"),
    ):
        commands.handle_command("/browser enable", "browser")

    assert refresh.call_args_list != []
    assert store.is_enabled() is True


def test_a_failed_refresh_still_records_consent(store):
    broken = RuntimeError("registry unavailable")
    with (
        patch("code_puppy.tools.get_available_tool_names", side_effect=broken),
        patch(EMIT + "success"),
    ):
        commands.handle_command("/browser enable", "browser")

    assert store.is_enabled() is True


def test_unknown_subcommand_prints_usage(store):
    with patch(EMIT + "error") as error:
        commands.handle_command("/browser nope", "browser")
    assert "/browser" in error.call_args[0][0]


def test_connect_requires_a_target(store):
    with patch(EMIT + "error") as error:
        commands.handle_command("/browser connect", "browser")
    assert "devtools-url" in error.call_args[0][0]


def test_connect_rejects_a_non_devtools_url(store):
    with patch(EMIT + "error") as error:
        commands.handle_command("/browser connect localhost:9222", "browser")
    assert "Invalid CDP endpoint" in error.call_args[0][0]
    assert store.endpoint() is None


def test_connect_saves_even_while_the_browser_is_asleep(store, monkeypatch):
    monkeypatch.setattr(
        commands.browser,
        "probe_endpoint",
        lambda url: browser.Endpoint(url, reachable=False, product="unreachable"),
    )
    with patch(EMIT + "warning") as warning:
        commands.handle_command("/browser connect http://127.0.0.1:9222", "browser")

    assert store.endpoint() == "http://127.0.0.1:9222"
    assert "nothing answered" in warning.call_args[0][0]


def test_connect_celebrates_a_live_endpoint(store, monkeypatch):
    monkeypatch.setattr(
        commands.browser,
        "probe_endpoint",
        lambda url: browser.Endpoint(url, reachable=True, product="Chrome/144"),
    )
    with patch(EMIT + "success") as success:
        commands.handle_command("/browser connect http://127.0.0.1:9222", "browser")

    assert "Chrome/144" in success.call_args[0][0]


def test_disconnect_returns_to_auto_discovery(store):
    store.set_endpoint("http://127.0.0.1:9222")
    with patch(EMIT + "success"):
        commands.handle_command("/browser disconnect", "browser")
    assert store.endpoint() is None


def test_recordings_rejects_an_unknown_action(store):
    with patch(EMIT + "error") as error:
        commands.handle_command("/browser recordings sideways", "browser")
    assert "recordings" in error.call_args[0][0]


def test_recordings_passes_through_to_the_harness(store, monkeypatch):
    fake = _installed(monkeypatch, FakeSubprocess(stdout="recordings: on\n"))
    with patch(EMIT + "info") as info:
        commands.handle_command("/browser recordings on", "browser")

    assert fake.argv[-2:] == ["recordings", "enable"]
    assert "on" in info.call_args[0][0]


def test_doctor_command_prints_the_report(store, monkeypatch):
    fake = _installed(monkeypatch, FakeSubprocess(stdout="all good\n"))
    with patch(EMIT + "info"), patch(EMIT + "success") as success:
        commands.handle_command("/browser doctor", "browser")

    assert fake.argv[-1] == "--doctor"
    assert "healthy" in success.call_args[0][0]


def test_doctor_command_without_an_install(store, monkeypatch):
    monkeypatch.setattr(cli, "executable", lambda: None)
    with patch(EMIT + "error") as error:
        commands.handle_command("/browser doctor", "browser")
    assert "uv tool install" in error.call_args[0][0]


def test_status_is_honest_about_a_firefox_only_machine(store, monkeypatch):
    monkeypatch.setattr(cli, "executable", lambda: None)
    monkeypatch.setattr(
        commands.browser,
        "detect_browsers",
        lambda: [
            browser.Browser("Firefox", "/Applications/Firefox.app", False, True),
        ],
    )
    monkeypatch.setattr(commands.browser, "reachable_endpoints", lambda extra=(): [])

    with patch(EMIT + "info") as info, patch(EMIT + "warning") as warning:
        commands.handle_command("/browser status", "browser")

    messages = [call[0][0] for call in info.call_args_list] + [
        call[0][0] for call in warning.call_args_list
    ]
    joined = "\n".join(messages)
    assert "not installed" in joined
    assert "No drivable browser" in joined
    assert "not drivable: Firefox" in joined
    assert "awaiting your one-time consent" in joined


def test_status_lists_a_live_endpoint_and_a_running_browser(store, monkeypatch):
    store.set_enabled(True)
    monkeypatch.setattr(cli, "executable", lambda: "/tools/browser-harness")
    monkeypatch.setattr(cli, "version", lambda: "0.1.10")
    monkeypatch.setattr(
        commands.browser,
        "reachable_endpoints",
        lambda extra=(): [
            browser.Endpoint("http://127.0.0.1:9222", True, "Chrome/144")
        ],
    )
    monkeypatch.setattr(
        commands.browser,
        "detect_browsers",
        lambda: [browser.Browser("Chrome", "/Applications/Chrome", True, True)],
    )

    with patch(EMIT + "info") as info:
        commands.handle_command("/browser status", "browser")

    joined = "\n".join(call[0][0] for call in info.call_args_list)
    assert "0.1.10" in joined
    assert "Chrome/144" in joined
    assert "Chrome: running" in joined
    assert "consent: enabled" in joined


def test_status_reports_an_ambient_endpoint_as_the_winning_one(store, monkeypatch):
    monkeypatch.setattr(cli, "executable", lambda: "/tools/browser-harness")
    monkeypatch.setattr(cli, "version", lambda: "0.1.10")
    monkeypatch.setattr(cli, "ambient_endpoint", lambda: "wss://cloud.example/cdp")
    monkeypatch.setattr(commands.browser, "reachable_endpoints", lambda extra=(): [])
    monkeypatch.setattr(commands.browser, "detect_browsers", lambda: [])

    with patch(EMIT + "info") as info:
        commands.handle_command("/browser status", "browser")

    joined = "\n".join(call[0][0] for call in info.call_args_list)
    assert "wss://cloud.example/cdp (from BU_CDP_WS/BU_CDP_URL)" in joined


def test_status_surfaces_a_broken_override(monkeypatch):
    monkeypatch.setattr(
        cli,
        "executable",
        lambda: (_ for _ in ()).throw(
            cli.policy.BrowserHarnessError("not an executable")
        ),
    )
    with patch(EMIT + "error") as error:
        commands.handle_command("/browser status", "browser")
    assert "not an executable" in error.call_args[0][0]


def test_install_help_lists_drivable_browsers(store, monkeypatch):
    monkeypatch.setattr(browser.platform, "system", lambda: "Darwin")
    with patch(EMIT + "info") as info:
        commands.handle_command("/browser install", "browser")

    joined = info.call_args[0][0]
    assert "brew install --cask google-chrome" in joined
    assert "firefox" not in joined.casefold()


@pytest.mark.parametrize("name", sorted(tools.REGISTRARS))
def test_every_tool_name_is_registered_once_and_matches_its_function(name):
    registered = _registered(tools.REGISTRARS[name], name)
    assert registered.__name__ == name
