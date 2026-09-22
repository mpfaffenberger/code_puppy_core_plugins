"""Tests for herdr pane-session auto-resume (``herdr/restore.py``).

code-puppy keeps its own ``pane_id -> session`` map because herdr stores no
native session reference for a non-official agent, then resumes that session
on startup so a restored pane comes back where it left off.

Covers resolution (herdr ref vs local map), the map round-trip, the resume
orchestration, and the ``handle_cli_args`` / startup gating in
``register_callbacks``.
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from code_puppy_core_plugins.herdr import restore
from tests.herdr_test_support import FakeClient


@contextmanager
def _stub_agent(agent):
    """Stub ``code_puppy.agents.agent_manager`` so resume can import lazily.

    Importing the real module drags in optional runtime deps the plugin test
    venv does not carry; we only need ``get_current_agent`` for this unit.
    """
    keys = ("code_puppy.agents", "code_puppy.agents.agent_manager")
    saved = {k: sys.modules.get(k) for k in keys}
    sys.modules["code_puppy.agents"] = saved["code_puppy.agents"] or types.ModuleType(
        "code_puppy.agents"
    )
    manager = types.ModuleType("code_puppy.agents.agent_manager")
    manager.get_current_agent = lambda: agent
    sys.modules["code_puppy.agents.agent_manager"] = manager
    try:
        yield
    finally:
        for key, previous in saved.items():
            if previous is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = previous


# --- store round-trip ------------------------------------------------------


def test_remember_then_resolve_from_store(tmp_path, monkeypatch):
    monkeypatch.setattr("code_puppy.config.STATE_DIR", str(tmp_path))
    restore.remember_session("w1:p1", "my_session", str(tmp_path / "my_session.json"))

    ref = restore._from_store("w1:p1")
    assert ref == ("my_session", str(tmp_path))
    # An unknown pane has no entry.
    assert restore._from_store("w1:p2") is None


def test_remember_overwrites_previous_session(tmp_path, monkeypatch):
    monkeypatch.setattr("code_puppy.config.STATE_DIR", str(tmp_path))
    restore.remember_session("w1:p1", "old", str(tmp_path / "old.json"))
    restore.remember_session("w1:p1", "new", str(tmp_path / "new.json"))
    assert restore._from_store("w1:p1") == ("new", str(tmp_path))


def test_forget_session_removes_entry(tmp_path, monkeypatch):
    monkeypatch.setattr("code_puppy.config.STATE_DIR", str(tmp_path))
    restore.remember_session("w1:p1", "s", str(tmp_path / "s.json"))
    restore.forget_session("w1:p1")
    assert restore._from_store("w1:p1") is None


def test_store_is_fail_soft_without_state_dir(monkeypatch):
    monkeypatch.setattr("code_puppy.config.STATE_DIR", "/nonexistent/\x00bad")
    # Must not raise; a broken store only costs us one auto-resume.
    restore.remember_session("w1:p1", "s", "/tmp/s.json")
    assert restore._from_store("w1:p1") is None


# --- resolution precedence -------------------------------------------------


def test_herdr_ref_wins_over_local_store(tmp_path, monkeypatch):
    monkeypatch.setattr("code_puppy.config.STATE_DIR", str(tmp_path))
    restore.remember_session("w1:p1", "local", str(tmp_path / "local.json"))
    client = FakeClient(
        pane_id="w1:p1",
        agent_session={"kind": "id", "value": "from_herdr"},
    )
    assert restore.resolve_stored_session(client) == ("from_herdr", "")


def test_herdr_path_kind_yields_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("code_puppy.config.STATE_DIR", str(tmp_path))
    client = FakeClient(
        pane_id="w1:p1",
        agent_session={"kind": "path", "value": "/elsewhere/sess.json"},
    )
    assert restore.resolve_stored_session(client) == ("sess", "/elsewhere")


def test_local_store_used_when_herdr_has_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("code_puppy.config.STATE_DIR", str(tmp_path))
    restore.remember_session("w1:p1", "local", str(tmp_path / "local.json"))
    client = FakeClient(pane_id="w1:p1", agent_session=None)
    assert restore.resolve_stored_session(client) == ("local", str(tmp_path))


# --- resume orchestration --------------------------------------------------


def _patch_load(history):
    return patch("code_puppy.session_storage.load_session", return_value=history)


def test_resume_loads_history_and_pins(tmp_path, monkeypatch):
    monkeypatch.setattr("code_puppy.config.STATE_DIR", str(tmp_path))
    monkeypatch.setattr("code_puppy.config.AUTOSAVE_DIR", str(tmp_path))
    restore.remember_session("w1:p1", "sess", str(tmp_path / "sess.json"))

    agent = MagicMock()
    pin = MagicMock()
    client = FakeClient(pane_id="w1:p1")
    with (
        _stub_agent(agent),
        _patch_load(["m1", "m2"]),
        patch("code_puppy.config.pin_current_session_name", pin),
        patch("code_puppy_core_plugins.herdr.restore._announce"),
    ):
        assert restore.resume_session(client) is True

    agent.set_message_history.assert_called_once_with(["m1", "m2"])
    pin.assert_called_once_with("sess")


def test_resume_no_reference_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr("code_puppy.config.STATE_DIR", str(tmp_path))
    client = FakeClient(pane_id="w1:p9")
    assert restore.resume_session(client) is False


def test_resume_empty_history_not_adopted(tmp_path, monkeypatch):
    monkeypatch.setattr("code_puppy.config.STATE_DIR", str(tmp_path))
    restore.remember_session("w1:p1", "sess", str(tmp_path / "sess.json"))
    monkeypatch.setattr("code_puppy.config.AUTOSAVE_DIR", str(tmp_path))
    client = FakeClient(pane_id="w1:p1")
    with _stub_agent(MagicMock()), _patch_load([]):
        assert restore.resume_session(client) is False


def test_resume_missing_file_forgets_entry(tmp_path, monkeypatch):
    monkeypatch.setattr("code_puppy.config.STATE_DIR", str(tmp_path))
    restore.remember_session("w1:p1", "gone", str(tmp_path / "gone.json"))
    monkeypatch.setattr("code_puppy.config.AUTOSAVE_DIR", str(tmp_path))
    client = FakeClient(pane_id="w1:p1")

    def _raise(*_a, **_k):
        raise FileNotFoundError("gone.json")

    with (
        _stub_agent(MagicMock()),
        patch("code_puppy.session_storage.load_session", side_effect=_raise),
    ):
        assert restore.resume_session(client) is False
    assert restore._from_store("w1:p1") is None


def test_resume_skipped_when_client_inactive():
    client = FakeClient(active=False, pane_id="w1:p1")
    assert restore.resume_session(client) is False


# --- register_callbacks gating ---------------------------------------------


def test_handle_cli_args_records_explicit_resume():
    from code_puppy_core_plugins.herdr import register_callbacks as rc

    args = MagicMock(resume="named-session", quick_resume=None, prompt=None)
    assert rc._on_handle_cli_args(args) is None
    assert rc._cli_resume_requested is True


def test_handle_cli_args_records_quick_resume():
    from code_puppy_core_plugins.herdr import register_callbacks as rc

    args = MagicMock(resume=None, quick_resume=".", prompt=None)
    rc._on_handle_cli_args(args)
    assert rc._cli_resume_requested is True


def test_handle_cli_args_records_headless():
    from code_puppy_core_plugins.herdr import register_callbacks as rc

    args = MagicMock(resume=None, quick_resume=None, prompt="do a thing")
    rc._on_handle_cli_args(args)
    assert rc._cli_headless is True


def test_auto_resume_yields_to_explicit_resume(monkeypatch):
    from code_puppy_core_plugins.herdr import register_callbacks as rc

    called = []
    monkeypatch.setattr(rc, "_cli_resume_requested", True)
    monkeypatch.setattr(rc, "_cli_headless", False)
    monkeypatch.setattr(rc.restore, "resume_session", lambda c: called.append(c))
    rc._maybe_auto_resume()
    assert called == []


def test_auto_resume_runs_when_unasked(monkeypatch):
    from code_puppy_core_plugins.herdr import register_callbacks as rc

    called = []
    monkeypatch.delenv("HERDR_NO_AUTO_RESUME", raising=False)
    monkeypatch.setattr(rc, "_cli_resume_requested", False)
    monkeypatch.setattr(rc, "_cli_headless", False)
    monkeypatch.setattr(rc.restore, "resume_session", lambda c: called.append(c))
    rc._maybe_auto_resume()
    assert called == [rc._client]


def test_auto_resume_honours_env_opt_out(monkeypatch):
    from code_puppy_core_plugins.herdr import register_callbacks as rc

    called = []
    monkeypatch.setenv("HERDR_NO_AUTO_RESUME", "1")
    monkeypatch.setattr(rc, "_cli_resume_requested", False)
    monkeypatch.setattr(rc, "_cli_headless", False)
    monkeypatch.setattr(rc.restore, "resume_session", lambda c: called.append(c))
    rc._maybe_auto_resume()
    assert called == []
