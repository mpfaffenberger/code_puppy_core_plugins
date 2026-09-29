"""Tests for the herdr plugin's fail-soft source adapters.

Covers the session-title seam: ``sources`` reads the session metadata
sidecar that the ``session_namer`` plugin maintains, and everything here
must degrade to ``None``/safe defaults instead of raising.
"""

from __future__ import annotations

import json

import code_puppy_core_plugins.herdr.sources as src


def _write_sidecar(tmp_path, session_name: str, meta: dict) -> None:
    (tmp_path / f"{session_name}_meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )


def _patch_session(monkeypatch, tmp_path, session_name: str | None) -> None:
    monkeypatch.setattr("code_puppy.config.AUTOSAVE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "code_puppy.config.get_current_session_name",
        lambda: session_name,
    )


# --- current_session_title --------------------------------------------------


def test_current_session_title_reads_sidecar(monkeypatch, tmp_path):
    _write_sidecar(tmp_path, "sess", {"title": "  Fix the flaky test  "})
    _patch_session(monkeypatch, tmp_path, "sess")
    assert src.current_session_title() == "Fix the flaky test"


def test_current_session_title_clips_to_herdr_limit(monkeypatch, tmp_path):
    _write_sidecar(tmp_path, "sess", {"title": "x" * 200})
    _patch_session(monkeypatch, tmp_path, "sess")
    assert src.current_session_title() == "x" * 80


def test_current_session_title_missing_sidecar_is_none(monkeypatch, tmp_path):
    _patch_session(monkeypatch, tmp_path, "nope")
    assert src.current_session_title() is None


def test_current_session_title_malformed_sidecar_is_none(monkeypatch, tmp_path):
    (tmp_path / "sess_meta.json").write_text("{not json", encoding="utf-8")
    _patch_session(monkeypatch, tmp_path, "sess")
    assert src.current_session_title() is None


def test_current_session_title_non_string_is_none(monkeypatch, tmp_path):
    _write_sidecar(tmp_path, "sess", {"title": 42})
    _patch_session(monkeypatch, tmp_path, "sess")
    assert src.current_session_title() is None


def test_current_session_title_blank_is_none(monkeypatch, tmp_path):
    _write_sidecar(tmp_path, "sess", {"title": "   "})
    _patch_session(monkeypatch, tmp_path, "sess")
    assert src.current_session_title() is None


def test_current_session_title_without_session_is_none(monkeypatch, tmp_path):
    _patch_session(monkeypatch, tmp_path, None)
    assert src.current_session_title() is None


# --- naming_enabled ---------------------------------------------------------


def test_naming_enabled_defaults_true(monkeypatch):
    monkeypatch.setattr("code_puppy.config.get_value", lambda key: None)
    assert src.naming_enabled() is True


def test_naming_enabled_config_gate(monkeypatch):
    for value, expected in [
        ("0", False),
        ("false", False),
        ("OFF", False),
        ("no", False),
        ("1", True),
        ("true", True),
        ("  ", True),
    ]:
        monkeypatch.setattr("code_puppy.config.get_value", lambda key, v=value: v)
        assert src.naming_enabled() is expected


def test_naming_enabled_fail_closed(monkeypatch):
    monkeypatch.setattr(
        "code_puppy.config.get_value",
        lambda key: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert src.naming_enabled() is False


# --- current_session_name / current_session_ref ------------------------------


def test_current_session_name_passthrough(monkeypatch, tmp_path):
    _patch_session(monkeypatch, tmp_path, "sess")
    assert src.current_session_name() == "sess"


def test_current_session_ref_unchanged_shape(monkeypatch, tmp_path):
    _patch_session(monkeypatch, tmp_path, "sess")
    ref = src.current_session_ref()
    assert ref is not None
    name, path = ref
    assert name == "sess"
    assert path.endswith("sess.pkl")


def test_current_session_ref_none_without_session(monkeypatch, tmp_path):
    _patch_session(monkeypatch, tmp_path, None)
    assert src.current_session_ref() is None
