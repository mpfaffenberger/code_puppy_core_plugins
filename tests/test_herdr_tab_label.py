"""Tests for the herdr workspace-tab label sync (``tab.get`` / ``tab.rename``).

Split out of ``test_herdr_client.py`` so each file stays well under the
600-line cap. Covers the mailbox enqueue/dedup logic, the shutdown drain
order (restore before release), and real ``AF_UNIX`` round-trips against a
fake herdr that answers ``tab.get`` and records ``tab.rename`` calls.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time

import pytest

import code_puppy_core_plugins.herdr.client as cl
from code_puppy_core_plugins.herdr.client import HerdrClient


def _inert_client(monkeypatch):
    """An inactive client whose worker never starts, for slot-logic unit tests.

    (Same helper as ``test_herdr_client.py`` -- kept local so each test
    file stands on its own.)
    """
    for var in ("HERDR_ENV", "HERDR_SOCKET_PATH", "HERDR_PANE_ID", "HERDR_TAB_ID"):
        monkeypatch.delenv(var, raising=False)
    c = HerdrClient()
    assert c.active is False
    assert c._worker is None
    return c


def test_tab_label_enqueue_and_dedup(monkeypatch):
    c = _inert_client(monkeypatch)
    c._active = True
    c._tab_id = "w1:t1"
    c.set_tab_label("Fix the flaky test")
    assert c._tab == {"op": "set", "label": "Fix the flaky test", "expected": None}
    # An identical set while the first is still queued is deduped.
    c.set_tab_label("Fix the flaky test")
    assert c._tab == {"op": "set", "label": "Fix the flaky test", "expected": None}
    # A new label replaces the pending job (latest wins).
    c.set_tab_label("Second title")
    assert c._tab == {"op": "set", "label": "Second title", "expected": None}
    # A restore request becomes its own job.
    c.set_tab_label(None, expected="Second title")
    assert c._tab == {"op": "restore", "label": None, "expected": "Second title"}


def test_tab_label_noop_without_tab_id(monkeypatch):
    c = _inert_client(monkeypatch)
    c._active = True
    c.set_tab_label("Some title")  # no HERDR_TAB_ID -> inert
    assert c._tab is None


def test_tab_label_restore_noop_when_never_renamed(monkeypatch):
    c = _inert_client(monkeypatch)
    c._active = True
    c._tab_id = "w1:t1"
    c.set_tab_label(None, expected="Old title")
    assert c._tab is None  # never renamed: nothing to restore, no job


def test_tab_restore_drains_before_release(monkeypatch):
    """The tab-label restore is shutdown work: it drains ahead of release."""
    c = _inert_client(monkeypatch)
    c._tab = {"op": "restore", "label": None, "expected": "X"}
    c._closing = True
    c._release = {}
    with c._cond:
        first = c._take_next_locked()
        second = c._take_next_locked()
    assert first[0] == cl._M_TAB_RENAME
    assert first[1]["op"] == "restore"
    assert second[0] == cl._M_RELEASE


class _FakeTabServer(threading.Thread):
    """Minimal herdr stand-in: answers tab.get, records tab.rename calls."""

    def __init__(self, sock_path: str, pane_count: int = 1) -> None:
        super().__init__(daemon=True)
        self.pane_count = pane_count
        self.label = "1"
        self.renames: list[str] = []
        self.methods: list[str] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(sock_path)
        self._server.listen(16)

    def run(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            try:
                data = conn.recv(65536)
                if not data:
                    continue
                env = json.loads(data.splitlines()[0])
                method = env["method"]
                self.methods.append(method)
                if method == "tab.get":
                    reply = {
                        "result": {
                            "tab": {
                                "tab_id": "w1:t1",
                                "label": self.label,
                                "pane_count": self.pane_count,
                            }
                        },
                        "type": "tab_info",
                    }
                else:
                    if method == "tab.rename":
                        self.renames.append(env["params"]["label"])
                        self.label = env["params"]["label"]
                    reply = {"result": {"type": "ok"}}
                conn.sendall((json.dumps(reply) + "\n").encode())
            finally:
                conn.close()

    def close(self) -> None:
        self._server.close()


def _wait_for_tab_label(server: "_FakeTabServer", label: str) -> bool:
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if server.label == label:
            return True
        time.sleep(0.02)
    return False


def _live_tab_client(monkeypatch, sock_path: str) -> HerdrClient:
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", sock_path)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    monkeypatch.setenv("HERDR_TAB_ID", "w1:t1")
    client = HerdrClient()
    assert client.active is True
    return client


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="AF_UNIX transport is unix-only"
)
def test_tab_label_rename_and_restore_over_socket(monkeypatch):
    tmp = tempfile.mkdtemp()
    sock_path = os.path.join(tmp, "herdr.sock")
    server = _FakeTabServer(sock_path)
    server.start()

    client = _live_tab_client(monkeypatch, sock_path)
    client.set_tab_label("Fix the flaky test")
    assert _wait_for_tab_label(server, "Fix the flaky test")
    client.set_tab_label(None, expected="Fix the flaky test")
    client.release_and_close(timeout_s=2.0)
    client._worker.join(timeout=2.0)
    server.close()

    assert server.renames == ["Fix the flaky test", "1"]
    assert server.label == "1"  # the original label was restored


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="AF_UNIX transport is unix-only"
)
def test_tab_label_skips_shared_tab(monkeypatch):
    """A tab shared with other panes is never relabelled by us."""
    tmp = tempfile.mkdtemp()
    sock_path = os.path.join(tmp, "herdr.sock")
    server = _FakeTabServer(sock_path, pane_count=2)
    server.start()

    client = _live_tab_client(monkeypatch, sock_path)
    client.set_tab_label("Fix the flaky test")
    deadline = time.time() + 2.0
    while time.time() < deadline and "tab.get" not in server.methods:
        time.sleep(0.02)
    assert "tab.get" in server.methods  # the gate ran...
    time.sleep(0.15)
    client.release_and_close(timeout_s=1.0)
    client._worker.join(timeout=1.0)
    server.close()
    assert server.renames == []  # ...but the rename never happened
    assert server.label == "1"


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="AF_UNIX transport is unix-only"
)
def test_tab_restore_leaves_manual_rename_alone(monkeypatch):
    """A tab the user renamed meanwhile is left as-is on exit."""
    tmp = tempfile.mkdtemp()
    sock_path = os.path.join(tmp, "herdr.sock")
    server = _FakeTabServer(sock_path)
    server.start()

    client = _live_tab_client(monkeypatch, sock_path)
    client.set_tab_label("Fix the flaky test")
    assert _wait_for_tab_label(server, "Fix the flaky test")
    server.label = "User's name"  # the user renamed the tab by hand
    client.set_tab_label(None, expected="Fix the flaky test")
    client.release_and_close(timeout_s=2.0)
    client._worker.join(timeout=2.0)
    server.close()
    assert server.renames == ["Fix the flaky test"]
    assert server.label == "User's name"
