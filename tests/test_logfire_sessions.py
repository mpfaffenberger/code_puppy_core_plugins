"""Tests for the logfire_sessions plugin."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from code_puppy_core_plugins.logfire_sessions import mirror, register_callbacks, sync


# --------------------------------------------------------------------- sync.py
def test_fingerprint_is_stable_and_content_sensitive():
    message = {"role": "user", "content": "hello"}
    assert sync.fingerprint(message) == sync.fingerprint(
        {"content": "hello", "role": "user"}
    )
    assert sync.fingerprint(message) != sync.fingerprint(
        {"role": "user", "content": "jello"}
    )


def test_plan_sync_cases():
    current = ["a", "b", "c"]
    assert sync.plan_sync(None, current) == 0
    assert sync.plan_sync(["a"], current) == 1  # append-only growth
    assert sync.plan_sync(current, current) == 3  # nothing new
    assert sync.plan_sync(current, current[:2]) == 0  # history shrank: full re-sync
    assert sync.plan_sync(["a", "x"], current) == 0  # diverged


def test_encode_decode_round_trip_with_chunking():
    big = "x" * 90_000
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": big},
    ]
    records = []
    for seq, message in enumerate(messages):
        records.extend(
            sync.encode_message_records(
                name="s",
                seq=seq,
                message=message,
                scope_key="/tmp/proj",
                project_name="proj",
                remote=None,
                branch=None,
            )
        )
    assert all(len(r["cp.hist.payload"]) <= sync.CHUNK_CHARS for r in records)

    rows = [
        {"attributes": attrs, "timestamp": float(i)} for i, attrs in enumerate(records)
    ]
    restored = sync.decode_message_rows(rows)
    assert restored == messages


def test_decode_prefers_latest_resync_of_same_seq():
    old = [
        {
            "attributes": {
                "cp.hist.name": "s",
                "cp.hist.seq": 0,
                "cp.hist.chunk": "0/1",
                "cp.hist.payload": _payload({"v": 1}),
            },
            "timestamp": 1.0,
        }
    ]
    new = [
        {
            "attributes": {
                "cp.hist.name": "s",
                "cp.hist.seq": 0,
                "cp.hist.chunk": "0/1",
                "cp.hist.payload": _payload({"v": 2}),
            },
            "timestamp": 2.0,
        }
    ]
    assert sync.decode_message_rows(old + new) == [{"v": 2}]
    assert sync.decode_message_rows(new + old) == [{"v": 2}]


def _payload(message: dict) -> str:
    """One-chunk payload in the same encoding encode_message_records uses."""
    return sync.encode_message_records(
        name="s",
        seq=0,
        message=message,
        scope_key=None,
        project_name=None,
        remote=None,
        branch=None,
    )[0]["cp.hist.payload"]


def test_state_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(sync, "state_path", lambda: path)
    state = sync.load_state()
    assert state == {"version": sync.STATE_VERSION, "sessions": {}}
    state["sessions"]["k"] = sync.synced_entry(["a", "b"])
    sync.save_state(state)
    assert sync.load_state()["sessions"]["k"]["fingerprints"] == ["a", "b"]
    path.write_text("not json")
    assert sync.load_state() == {"version": sync.STATE_VERSION, "sessions": {}}


# ------------------------------------------------------------------ mirror.py
@pytest.fixture()
def fake_logfire(monkeypatch):
    mock = MagicMock()
    monkeypatch.setitem(sys.modules, "logfire", mock)
    monkeypatch.setattr(mirror, "_CONFIGURED", True)
    return mock


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "state_path", lambda: tmp_path / "sync.json")
    monkeypatch.setattr(
        mirror,
        "workspace_metadata",
        lambda: {
            "scope_key": "/w",
            "project_name": "proj",
            "remote": "https://example.git",
            "branch": "main",
        },
    )


def _session(tmp_path, name="demo"):
    from code_puppy.session_storage import (
        build_envelope,
        write_envelope_file,
    )

    history = [{"role": "user", "content": f"msg {name}"}]
    json_path = tmp_path / f"{name}.json"
    write_envelope_file(json_path, build_envelope(history))
    return SimpleNamespace(session_name=name, json_path=json_path, scope_key="/w")


def test_mirror_emits_new_messages_then_dedupes(tmp_path, isolated, fake_logfire):
    metadata = _session(tmp_path)
    emitted, total = mirror.mirror_session(metadata)
    assert (emitted, total) == (1, 1)
    info_calls = [
        c for c in fake_logfire.info.call_args_list if c.args[0] == "cp.hist.message"
    ]
    assert len(info_calls) == 1
    attrs = info_calls[0].kwargs
    assert attrs["cp.hist.name"] == "demo"
    assert attrs["cp.project.remote"] == "https://example.git"

    emitted_again, _ = mirror.mirror_session(metadata)
    assert emitted_again == 0


def test_mirror_full_resync_after_divergence(tmp_path, isolated, fake_logfire):
    metadata = _session(tmp_path)
    mirror.mirror_session(metadata)

    from code_puppy.session_storage import build_envelope, write_envelope_file

    diverged = [{"role": "user", "content": "rewritten"}]
    write_envelope_file(metadata.json_path, build_envelope(diverged))
    metadata.message_count = 1

    emitted, total = mirror.mirror_session(metadata)
    assert (emitted, total) == (1, 1)


# ------------------------------------------------------- register_callbacks.py
def test_command_routing(tmp_path, isolated, monkeypatch):
    monkeypatch.setattr(register_callbacks, "emit_success", print)
    monkeypatch.setattr(register_callbacks, "emit_info", print)
    monkeypatch.setattr(register_callbacks, "emit_warning", print)
    monkeypatch.setattr(register_callbacks, "emit_error", print)

    assert register_callbacks._handle("/other thing", "other") is None

    calls = {}
    monkeypatch.setattr(
        "code_puppy.config.set_value",
        lambda key, value: calls.__setitem__(key, value),
    )
    assert (
        register_callbacks._handle("/logfire-sessions on", "logfire-sessions") is True
    )
    assert calls == {"enable_logfire_sessions": "true"}

    monkeypatch.setattr(
        "code_puppy_core_plugins.logfire_sessions.query.list_sessions",
        lambda limit=25: [],
    )
    register_callbacks._handle("/logfire-sessions list", "logfire-sessions")

    # Unknown action: usage hint, still owned by us.
    assert (
        register_callbacks._handle("/logfire-sessions bogus", "logfire-sessions")
        is True
    )


def test_post_autosave_swallows_errors(monkeypatch):
    def boom(_metadata):
        raise RuntimeError("no network")

    monkeypatch.setattr(
        "code_puppy.config.get_value",
        lambda key: "true" if key == "enable_logfire_sessions" else None,
    )
    monkeypatch.setattr(sys, "stderr", open("/dev/null", "w"))  # silence traceback
    monkeypatch.setattr(
        "code_puppy_core_plugins.logfire_sessions.mirror.mirror_session", boom
    )
    register_callbacks._on_post_autosave(SimpleNamespace())  # must not raise
