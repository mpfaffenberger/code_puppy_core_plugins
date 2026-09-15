"""Live smoke test for the herdr integration against a real herdr pane.

This is a **manual** verification tool, not part of the CI contract (the
unit tests in ``tests/test_herdr_*.py`` are). It exercises every method
the plugin uses -- ``pane.report_agent``, ``pane.report_agent_session``,
``pane.report_metadata`` (including the presentation ``title``),
``pane.release_agent``, and ``tab.get`` / ``tab.rename`` -- against a live
herdr server, and reads each result back with ``herdr pane get`` /
``herdr tab get`` to prove herdr actually stored it.

It is safe to run: it creates its OWN disposable pane and tab, drives the
real ``HerdrClient`` against them, and always closes both again -- your
working pane and tabs are never touched.

Usage (from inside a herdr pane)::

    python -m code_puppy_core_plugins.herdr.smoke

Requires the ``herdr`` CLI on ``PATH`` and the ``HERDR_ENV`` /
``HERDR_SOCKET_PATH`` / ``HERDR_PANE_ID`` variables herdr injects. Exits 0
on success, 1 on any failed check, 2 on setup failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, Optional

_SPLIT_RATIO = 0.12
_SETTLE_S = 0.6


def _herdr(*args: str) -> Dict[str, Any]:
    """Run a herdr CLI command and parse its JSON reply."""
    proc = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=10)
    if not proc.stdout.strip():
        raise RuntimeError(f"herdr {' '.join(args)} produced no output: {proc.stderr}")
    return json.loads(proc.stdout)


def _pane_field(pane_id: str, field: str) -> Any:
    pane = _herdr("pane", "get", pane_id)["result"]["pane"]
    return pane.get(field)


def _tab_field(tab_id: str, field: str) -> Any:
    tab = _herdr("tab", "get", tab_id)["result"]["tab"]
    return tab.get(field)


def _create_disposable_pane(parent: str) -> str:
    reply = _herdr(
        "pane",
        "split",
        parent,
        "--direction",
        "down",
        "--ratio",
        str(_SPLIT_RATIO),
        "--no-focus",
    )
    return reply["result"]["pane"]["pane_id"]


def _make_client(pane_id: str, tab_id: Optional[str] = None):
    # Point a real client at the disposable pane. HERDR_SOCKET_PATH is
    # inherited from the surrounding pane.
    os.environ["HERDR_ENV"] = "1"
    os.environ["HERDR_PANE_ID"] = pane_id
    if tab_id is not None:
        os.environ["HERDR_TAB_ID"] = tab_id
    from code_puppy_core_plugins.herdr.client import HerdrClient

    return HerdrClient()


def _run_checks(client, pane_id: str) -> Dict[str, bool]:
    results: Dict[str, bool] = {}

    client.report_state("working")
    client.report_session("smoke_session_x", "/tmp/smoke_session_x.pkl")
    client.report_metadata(
        {"model": "claude-smoke", "context": "42%", "tokens": "48k/200k"}
    )
    time.sleep(_SETTLE_S)
    results["state_working"] = _pane_field(pane_id, "agent_status") == "working"
    results["metadata_tokens"] = bool(_pane_field(pane_id, "tokens"))

    client.report_metadata(
        {"model": "claude-smoke", "context": "42%", "tokens": "48k/200k"},
        title="herdr smoke title",
    )
    time.sleep(_SETTLE_S)
    results["metadata_title"] = _pane_field(pane_id, "title") == "herdr smoke title"

    client.report_metadata(None, clear_title=True)
    time.sleep(_SETTLE_S)
    results["metadata_title_cleared"] = _pane_field(pane_id, "title") is None

    client.report_state("blocked")
    time.sleep(_SETTLE_S)
    results["state_blocked"] = _pane_field(pane_id, "agent_status") == "blocked"

    client.report_state("idle")
    time.sleep(_SETTLE_S)
    # An unacknowledged idle transition shows as ``done`` in herdr 0.8.x
    # ("finished, awaiting your attention"); once seen it settles to
    # ``idle``.
    results["state_idle"] = _pane_field(pane_id, "agent_status") in ("done", "idle")

    started = time.monotonic()
    client.release_and_close(timeout_s=1.0)
    elapsed = time.monotonic() - started
    results["release_bounded"] = elapsed < 1.5
    time.sleep(_SETTLE_S)
    # Releasing authority drops the pane back to 'unknown'.
    results["release_clears_state"] = _pane_field(pane_id, "agent_status") == "unknown"

    return results


def _run_tab_checks(workspace: str, pane_id: str) -> Dict[str, bool]:
    """Tab-label sync on a disposable tab: rename + exit-time restore.

    Uses its own tab (single pane, so the ownership gate passes) and its
    own client; the pane passed in is only a valid pane_id for the client
    to point at -- the tab jobs never touch it.
    """
    results: Dict[str, bool] = {}
    created = _herdr(
        "tab", "create", "--workspace", workspace, "--label", "herdr smoke tab"
    )
    tab_id = created["result"]["tab"]["tab_id"]
    try:
        client = _make_client(pane_id, tab_id=tab_id)
        if not client.active:
            print("Tab client did not activate.", file=sys.stderr)
            return results
        client.set_tab_label("herdr smoke title")
        time.sleep(_SETTLE_S)
        results["tab_label_set"] = _tab_field(tab_id, "label") == "herdr smoke title"
        client.set_tab_label(None, expected="herdr smoke title")
        client.release_and_close(timeout_s=1.0)
        time.sleep(_SETTLE_S)
        # The exit-time restore puts the tab back the way we found it.
        results["tab_label_restored"] = _tab_field(tab_id, "label") == "herdr smoke tab"
    finally:
        try:
            _herdr("tab", "close", tab_id)
            print(f"Closed disposable tab {tab_id}")
        except Exception as exc:  # pragma: no cover - manual tool
            print(f"WARNING: could not close {tab_id}: {exc}", file=sys.stderr)
    return results


def main() -> int:
    if os.environ.get("HERDR_ENV") != "1":
        print("Not inside a herdr pane (HERDR_ENV != 1). Aborting.", file=sys.stderr)
        return 2
    parent = os.environ.get("HERDR_PANE_ID")
    if not parent:
        print("HERDR_PANE_ID is unset. Aborting.", file=sys.stderr)
        return 2
    workspace = os.environ.get("HERDR_WORKSPACE_ID") or parent.split(":")[0]

    pane_id: Optional[str] = None
    try:
        pane_id = _create_disposable_pane(parent)
        print(f"Created disposable pane {pane_id} (from {parent})")
        client = _make_client(pane_id)
        if not client.active:
            print("HerdrClient did not activate. Aborting.", file=sys.stderr)
            return 2
        results = _run_checks(client, pane_id)
        results.update(_run_tab_checks(workspace, pane_id))
    except Exception as exc:  # pragma: no cover - manual tool
        print(f"Smoke setup/run error: {exc}", file=sys.stderr)
        return 2
    finally:
        if pane_id:
            try:
                _herdr("pane", "close", pane_id)
                print(f"Closed disposable pane {pane_id}")
            except Exception as exc:  # pragma: no cover - manual tool
                print(f"WARNING: could not close {pane_id}: {exc}", file=sys.stderr)

    print("\nResults:")
    ok = True
    for name, passed in results.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
        ok = ok and passed
    print("\nSMOKE " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
