"""Thread-safe registry of active sub-agents + status derivation.

The puppy spinner repaints from a background thread ~20x/second, so all reads
here must be cheap and lock-guarded.

Registration is driven by the sub-agent INVOCATION banner (which carries the
exact name, session_type, model AND session_id), so attribution is exact even
for parallel sub-agents -- no FIFO guessing, no session-id parsing. Status
updates (``record_event``) are UPDATE-ONLY: an event for an unregistered
session is ignored, which cleanly filters out the MAIN agent's own stream
events (the main agent is never registered).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional

# session_id -> {session_id, parent, name, model, status, start, last_seen}
_AGENTS: "Dict[str, Dict[str, Any]]" = {}
_LOCK = threading.RLock()

# --- tunables (env-overridable) -------------------------------------------
IDLE_PRUNE_S = float(os.environ.get("SUBAGENT_PANEL_IDLE_S", "600"))

# Single-char braille spinner frames (defined via escapes to keep the source
# emoji-free; braille isn't emoji but escapes dodge any filter ambiguity).
SPINNER_FRAMES = [
    "\u280b",
    "\u2819",
    "\u2839",
    "\u2838",
    "\u283c",
    "\u2834",
    "\u2826",
    "\u2827",
    "\u2807",
    "\u280f",
]


# Registration / teardown (invocation + response banners)
def register(
    session_id: Optional[str],
    name: str,
    model: Optional[str] = None,
    parent: Optional[str] = None,
    is_fork: bool = False,
    background: bool = False,
) -> None:
    if not session_id:
        return
    now = time.time()
    with _LOCK:
        # Re-invocation of an existing session: keep its original start time.
        existing = _AGENTS.get(session_id)
        _AGENTS[session_id] = {
            "session_id": session_id,
            "parent": parent,
            "name": name,
            "model": model,
            "is_fork": is_fork,
            "background": background,
            "status": "starting",
            "start": existing["start"] if existing else now,
            "last_seen": now,
        }


def finish(session_id: Optional[str]) -> None:
    if not session_id:
        return
    with _LOCK:
        _AGENTS.pop(session_id, None)


def mark_done(session_id: Optional[str]) -> None:
    """Mark a sub-agent completed but KEEP it in the live tree (frozen) until its
    root flushes. This avoids the vanish-then-reappear gap that happens if a
    nested child is popped the instant it finishes while its parent runs on."""
    if not session_id:
        return
    now = time.time()
    with _LOCK:
        entry = _AGENTS.get(session_id)
        if entry is None:
            return
        entry["done"] = True
        entry["status"] = "completed"
        entry["end"] = now
        entry["last_seen"] = now


def mark_failed(session_id: Optional[str]) -> None:
    """Mark a sub-agent FAILED (errored after exhausting retries) but KEEP it in
    the live tree until its root flushes -- same lifecycle as mark_done, but the
    renderer paints it red 'failed' with an X instead of green 'completed'."""
    if not session_id:
        return
    now = time.time()
    with _LOCK:
        entry = _AGENTS.get(session_id)
        if entry is None:
            return
        entry["done"] = True
        entry["failed"] = True
        entry["status"] = "failed"
        entry["end"] = now
        entry["last_seen"] = now


def clear() -> None:
    """Drop ALL tracked sub-agents -- detached ones included. Reserved for the
    cancel path (Ctrl+C takes forks/background agents down too) and for the
    runtime off-switch; ordinary end-of-turn cleanup is ``pop_settled``."""
    with _LOCK:
        _AGENTS.clear()


# Detached trees (/fork + background) outlive the turn that started them
def is_detached(entry: Dict[str, Any]) -> bool:
    """A ``/fork`` or ``background=True`` launch: its lifetime is its own task,
    not the main agent's turn, so end-of-turn cleanup must leave it alone."""
    return bool(entry.get("is_fork") or entry.get("background"))


def _root_id(agents: Dict[str, Dict[str, Any]], session_id: str) -> str:
    """Walk the parent chain to the tree root. A parent that isn't registered
    (the main agent, or None) makes the row a root. Cycle-safe."""
    seen = set()
    while True:
        parent = agents[session_id].get("parent")
        if not parent or parent not in agents or parent in seen:
            return session_id
        seen.add(session_id)
        session_id = parent


def _live_detached_ids(agents: Dict[str, Dict[str, Any]]) -> set:
    """Session ids of every row (root + descendants) in a detached tree whose
    root is still running."""
    live = set()
    for sid in agents:
        root = agents[_root_id(agents, sid)]
        if is_detached(root) and not root.get("done"):
            live.add(sid)
    return live


def foreground_busy() -> bool:
    """True while anything OUTSIDE a live detached tree is still running --
    i.e. the foreground swarm hasn't gone idle yet, so nothing may flush."""
    with _LOCK:
        live = _live_detached_ids(_AGENTS)
        return any(
            not entry.get("done") for sid, entry in _AGENTS.items() if sid not in live
        )


def pop_settled() -> List[Dict[str, Any]]:
    """Remove and return every row NOT in a live detached tree (oldest first).

    Live detached trees stay registered: a fork or background agent keeps
    its live row across turn boundaries until it finishes at its own
    boundary (``finish`` / ``mark_done``). Everything else -- the foreground
    swarm, plus detached trees that already completed -- is handed back to
    the caller to flush as frozen records (or discard at end of turn, when a
    root that errored/was cancelled never flushed).
    """
    with _LOCK:
        live = _live_detached_ids(_AGENTS)
        settled = [entry for sid, entry in _AGENTS.items() if sid not in live]
        for entry in settled:
            _AGENTS.pop(entry["session_id"], None)
        return sorted(settled, key=lambda e: e["start"])


# Live status (stream_event updates only)
def record_event(session_id: Optional[str], event_type: str, event_data: Any) -> None:
    if not session_id:
        return
    status = _derive_status(event_type, event_data)
    now = time.time()
    with _LOCK:
        entry = _AGENTS.get(session_id)
        if entry is None:
            return  # not a registered sub-agent (e.g. the main agent) -> ignore
        entry["last_seen"] = now
        if status:
            entry["status"] = status


# Reads
def snapshot() -> List[Dict[str, Any]]:
    """Return active sub-agents (oldest first), pruning idle/stale ones.

    DONE entries are never pruned by idle -- they stay in the live tree (shown
    as 'completed') until their root flushes them, so a finished child never
    vanishes mid-run.

    PARENTS are never idle-pruned either: a parent that has invoked children and
    is merely AWAITING them emits no stream events of its own, so its last_seen
    goes stale -- but it is busy, not idle. Pruning it would orphan its whole
    subtree, making the children re-render as depth-0 roots.

    ROOT rows (parent=None) are never idle-pruned either. A root is the visible
    anchor of a live user-initiated ``invoke_agent`` call; silently deleting it
    while its work is still in flight (e.g. a slow tool call that emits no
    stream events for many minutes) would strand any completed sibling in state
    with no flush trigger, and hide from the user that a real invocation is
    still running. Roots exit via one of three legitimate paths only:
    completion (``_handle_frozen`` -> ``mark_done`` -> ``_maybe_flush_group``),
    cancel (``state.clear()`` from ``_on_agent_run_cancel``), or end-of-turn
    (``state.clear()`` from ``_on_agent_run_end``).

    So only childless, not-done, genuinely-stale ORPHAN LEAVES are eligible --
    a non-root row whose parent is no longer in the tree AND has been silent
    longer than IDLE_PRUNE_S. End-of-turn ``state.clear()`` remains the real
    cleanup for anything that errors/cancels without flushing.
    """
    now = time.time()
    with _LOCK:
        ids = set(_AGENTS)
        # session_ids still referenced as someone's parent == busy (awaiting kids).
        busy_parents = {e.get("parent") for e in _AGENTS.values() if e.get("parent")}
        # Prune only non-done, childless, non-root orphan leaves whose parent is
        # gone and whose silence exceeds IDLE_PRUNE_S; roots need explicit cleanup.
        stale = [
            s
            for s, e in _AGENTS.items()
            if not e.get("done")
            and s not in busy_parents
            and e.get("parent") is not None
            and e.get("parent") not in ids
            and now - e["last_seen"] > IDLE_PRUNE_S
        ]
        for s in stale:
            _AGENTS.pop(s, None)
        return sorted(_AGENTS.values(), key=lambda e: e["start"])


def has_active() -> bool:
    with _LOCK:
        return bool(_AGENTS)


# Helpers
def _derive_status(event_type: str, event_data: Any) -> Optional[str]:
    """Map a sub-agent stream event to a short status. None = no change."""
    if not isinstance(event_data, dict):
        return None
    if event_type == "part_start":
        tool = event_data.get("tool_name")
        if tool:
            return f"calling {tool}"
        part_type = event_data.get("part_type", "") or ""
        if "Thinking" in part_type:
            return "thinking..."
        if "Text" in part_type:
            return "writing response"
    return None


def fmt_elapsed(start: float) -> str:
    """mm:ss elapsed (2-digit minutes), e.g. 00:19."""
    elapsed = int(max(0.0, time.time() - start))
    return f"{elapsed // 60:02d}:{elapsed % 60:02d}"


def fmt_elapsed_entry(entry: Dict[str, Any]) -> str:
    """mm:ss for an entry, frozen at its 'end' time once done."""
    end = entry.get("end")
    start = entry["start"]
    elapsed = int(max(0.0, (end if end else time.time()) - start))
    return f"{elapsed // 60:02d}:{elapsed % 60:02d}"


def spinner_frame() -> str:
    """Wall-clock-derived spinner char (~10fps), independent of caller."""
    return SPINNER_FRAMES[int(time.time() * 10) % len(SPINNER_FRAMES)]


def status_style(status: str) -> str:
    """Color-code the status text by activity type."""
    if status.startswith("calling"):
        return "yellow"
    if status.startswith("thinking"):
        return "magenta"
    if status.startswith("writing"):
        return "green"
    return "dim"
