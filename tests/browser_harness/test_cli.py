"""Subprocess plumbing for the browser-harness plugin."""

from __future__ import annotations

import subprocess

import pytest

from code_puppy_core_plugins.browser_harness import cli
from code_puppy_core_plugins.browser_harness.policy import BrowserHarnessError

from ._helpers import FakeSubprocess


def test_executable_prefers_the_environment_override(tmp_path, monkeypatch):
    override = tmp_path / "bh"
    override.write_text("#!/bin/sh\n")
    monkeypatch.setenv(cli.EXECUTABLE_ENV_VAR, str(override))
    assert cli.executable() == str(override)


def test_executable_shouts_about_a_broken_override(monkeypatch):
    monkeypatch.setenv(cli.EXECUTABLE_ENV_VAR, "definitely-not-here")
    with pytest.raises(BrowserHarnessError, match="not an executable"):
        cli.executable()


def test_executable_finds_the_path_and_the_uv_tool_install(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/usr/local/bin/browser-harness"
    )
    assert cli.executable() == "/usr/local/bin/browser-harness"

    missing = tmp_path / "uv-tool-bin" / "browser-harness"
    missing.parent.mkdir()
    missing.write_text("#!/bin/sh\n")
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "_UV_TOOL_BIN", missing)
    assert cli.executable() == str(missing)
    assert cli.installed() is True


def test_missing_install_reports_the_install_command(monkeypatch):
    monkeypatch.setattr(cli, "executable", lambda: None)
    with pytest.raises(BrowserHarnessError, match="uv tool install"):
        cli.require_executable()
    assert cli.version() is None


def test_environment_maps_an_http_endpoint(store, monkeypatch):
    store.set_endpoint("http://127.0.0.1:9222")
    env = cli.environment()
    assert env["BU_CDP_URL"] == "http://127.0.0.1:9222"
    assert "BU_CDP_WS" not in env


def test_environment_maps_a_websocket_endpoint(store):
    store.set_endpoint("wss://browser.example/cdp")
    assert cli.environment()["BU_CDP_WS"] == "wss://browser.example/cdp"


def test_an_ambient_endpoint_outranks_the_saved_one(store, monkeypatch):
    store.set_endpoint("http://127.0.0.1:9222")
    monkeypatch.setenv("BU_CDP_WS", "wss://somewhere.else/cdp")
    env = cli.environment()
    assert "BU_CDP_URL" not in env
    assert env["BU_CDP_WS"] == "wss://somewhere.else/cdp"
    assert cli.ambient_endpoint() == "wss://somewhere.else/cdp"


def test_environment_names_a_daemon_and_rejects_rubbish(store):
    assert cli.environment("r7k2")["BU_NAME"] == "r7k2"
    with pytest.raises(BrowserHarnessError, match="Invalid browser name"):
        cli.environment("../escape")


def test_run_script_hands_the_script_over_stdin(harness_bin, monkeypatch):
    fake = FakeSubprocess(stdout="page title\n")
    monkeypatch.setattr(cli.subprocess, "run", fake)
    result = cli.run_script("print(page_info())", "r7k2", 30.0)

    assert result.ok and result.stdout == "page title\n"
    assert fake.argv == [str(harness_bin)]
    assert fake.calls[0][1]["input"] == "print(page_info())"
    # Passing stdin= alongside input= is a ValueError in subprocess.run.
    assert "stdin" not in fake.calls[0][1]
    assert fake.env["BU_NAME"] == "r7k2"
    assert fake.calls[0][1]["timeout"] == 30.0


def test_run_command_never_blocks_on_stdin(harness_bin, monkeypatch):
    fake = FakeSubprocess(stdout="healthy\n")
    monkeypatch.setattr(cli.subprocess, "run", fake)
    result = cli.run_command(["--doctor"])

    assert result.ok
    assert fake.argv[-1] == "--doctor"
    assert fake.stdin == subprocess.DEVNULL


def test_timeouts_become_a_readable_failure(harness_bin, monkeypatch):
    error = subprocess.TimeoutExpired(
        cmd="browser-harness", timeout=1, output=b"partial"
    )
    fake = FakeSubprocess(error=error)
    monkeypatch.setattr(cli.subprocess, "run", fake)
    result = cli.run_script("wait(999)")

    assert result.ok is False and result.timed_out is True
    assert "partial" in result.stdout
    assert "timed out" in result.failure()


def test_output_is_capped_so_one_page_cannot_flood_the_context(
    harness_bin, monkeypatch
):
    fake = FakeSubprocess(stdout="x" * (cli.MAX_CAPTURED_CHARS + 500))
    monkeypatch.setattr(cli.subprocess, "run", fake)
    assert cli.run_script("print(1)").stdout.endswith("…")


def test_version_is_read_from_the_cli(monkeypatch):
    monkeypatch.setattr(cli, "executable", lambda: "/tools/browser-harness")
    fake = FakeSubprocess(stdout="0.1.10\n")
    monkeypatch.setattr(cli.subprocess, "run", fake)
    assert cli.version() == "0.1.10"
    assert fake.argv == ["/tools/browser-harness", "--version"]


@pytest.mark.parametrize(
    "stderr, needle",
    [
        (
            "RuntimeError: permission-blocked: Chrome is reachable",
            "mac-approve",
        ),
        (
            "remote debugging is turned off for this browser instance",
            "chrome://inspect",
        ),
        (
            "DevToolsActivePort not found in [...]",
            "/browser connect",
        ),
        ("chrome-not-running: no supported Chromium-family browser", "start one"),
    ],
)
def test_known_harness_errors_carry_their_documented_fix(stderr, needle):
    assert cli.fixup_for(stderr) is not None
    assert needle.lower() in cli.fixup_for(stderr).lower()


def test_unknown_errors_get_no_invented_fix():
    assert cli.fixup_for("RuntimeError: selector matched nothing") is None
