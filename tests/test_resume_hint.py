"""Tests for the resume_hint plugin (exit-time ``--resume`` breadcrumb)."""

from __future__ import annotations

import argparse
import shlex
import sys
from importlib.resources import files

import pytest
from code_puppy.i18n import get_locale, register_plugin_catalog, set_locale
from code_puppy.plugins import _plugin_loading_context

from code_puppy_core_plugins.resume_hint import register_callbacks as plugin

SESSION = "auto_session_20261020_132142_000001_4242"


@pytest.fixture
def autosave_dir(tmp_path, monkeypatch):
    # Register exactly as the loader does, so namespace rules are enforced.
    with _plugin_loading_context("resume_hint"):
        assert register_plugin_catalog(files(plugin.__package__).joinpath("locales"))
    monkeypatch.setattr(plugin, "AUTOSAVE_DIR", str(tmp_path))
    monkeypatch.setattr(plugin, "get_current_session_name", lambda: SESSION)
    monkeypatch.setattr(plugin, "get_headless_mode", lambda: False)
    monkeypatch.setattr(plugin, "get_auto_save_session", lambda: True)
    # Launched via the ``pup`` script: the hint must still say uvx code-puppy.
    monkeypatch.setattr(sys, "argv", ["/some/venv/bin/pup"])
    return tmp_path


@pytest.mark.parametrize("suffix", [".json", ".pkl"])
def test_prints_hint_when_session_saved(autosave_dir, capsys, suffix):
    (autosave_dir / f"{SESSION}{suffix}").write_text("{}")

    plugin._on_shutdown()

    out = capsys.readouterr().out
    assert "Resume this session with:" in out
    assert f"  uvx code-puppy --resume {SESSION}" in out


@pytest.mark.parametrize(
    ("locale", "header"),
    [
        ("en-US", "Resume this session with:"),
        ("es", "Reanuda esta sesión con:"),
        ("fr-CA", "Reprenez cette session avec :"),
    ],
)
def test_header_is_translated(autosave_dir, capsys, locale, header):
    (autosave_dir / f"{SESSION}.json").write_text("{}")
    previous_locale = get_locale()
    try:
        set_locale(locale)
        plugin._on_shutdown()
    finally:
        set_locale(previous_locale)

    assert capsys.readouterr().out.startswith(f"\n{header}\n")


def test_silent_when_session_never_saved(autosave_dir, capsys):
    plugin._on_shutdown()

    assert capsys.readouterr().out == ""


def test_silent_in_headless_mode(autosave_dir, capsys, monkeypatch):
    (autosave_dir / f"{SESSION}.json").write_text("{}")
    monkeypatch.setattr(plugin, "get_headless_mode", lambda: True)

    plugin._on_shutdown()

    assert capsys.readouterr().out == ""


def test_silent_when_autosave_disabled(autosave_dir, capsys, monkeypatch):
    # A file from /compact or an earlier --resume would be a stale snapshot.
    (autosave_dir / f"{SESSION}.json").write_text("{}")
    monkeypatch.setattr(plugin, "get_auto_save_session", lambda: False)

    plugin._on_shutdown()

    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "dependency",
    ["get_current_session_name", "get_auto_save_session", "get_headless_mode"],
)
def test_lookup_failure_never_raises(autosave_dir, capsys, monkeypatch, dependency):
    # e.g. puppy.cfg turning unreadable mid-session makes config reads raise.
    def boom():
        raise PermissionError("config exploded")

    monkeypatch.setattr(plugin, dependency, boom)

    plugin._on_shutdown()  # must not raise on the exit path

    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("session_name", [SESSION, "-dash-first"])
def test_resume_arg_parses_back_to_the_session_name(session_name):
    # Mirrors cli_runner's --resume definition; argparse eats a bare "-x" value.
    parser = argparse.ArgumentParser(exit_on_error=False)
    parser.add_argument("--resume", "-r", type=str)

    args = parser.parse_args(shlex.split(plugin._resume_arg(session_name)))

    assert args.resume == session_name
