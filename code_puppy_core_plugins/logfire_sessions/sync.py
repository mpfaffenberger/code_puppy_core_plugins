"""Pure encode/decode/state logic for mirroring sessions into Logfire.

No I/O beyond the sync-state JSON file, so everything here is unit-testable
without a Logfire project or network.

Record shape (one log record per message chunk)::

    cp.hist.name       session file stem
    cp.hist.seq        message index within the history
    cp.hist.chunk      "i/n" -- gzipped base64 payload chunk i of n
    cp.hist.payload    one base64(gzip(json(message))) chunk
    cp.hist.scope_key  resolved cwd of the session (compute_scope_key)
    cp.project.name / remote / branch   best-effort workspace metadata

Restore-side note: histories can be re-synced after compaction, so Logfire may
hold multiple versions of the same seq. :func:`decode_message_rows` therefore
deduplicates by ``seq``, keeping the *latest* row.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterable

CHUNK_CHARS = 16_000  # conservative attribute-size budget per record
STATE_VERSION = 1


def fingerprint(message: Any) -> str:
    """Stable sha256 for one jsonable pydantic-ai message."""
    canonical = json.dumps(message, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def plan_sync(previous: list[str] | None, current: list[str]) -> int:
    """Return the index in ``current`` to start emitting from.

    Append-only growth continues where the last sync stopped. Anything else
    (divergence from compaction/branching, or a shrunken history) triggers a
    full re-emit; Logfire keeps both generations and restore dedupes by seq.
    """
    if not previous:
        return 0
    overlap = min(len(previous), len(current))
    if previous[:overlap] == current[:overlap]:
        return max(overlap, 0) if len(current) >= len(previous) else 0
    return 0


def _chunk_b64(payload: bytes) -> list[str]:
    blob = base64.b64encode(gzip.compress(payload)).decode()
    return [blob[i : i + CHUNK_CHARS] for i in range(0, len(blob), CHUNK_CHARS)]


def encode_message_records(
    *,
    name: str,
    seq: int,
    message: Any,
    scope_key: str | None,
    project_name: str | None,
    remote: str | None,
    branch: str | None,
) -> list[dict[str, Any]]:
    """Encode one jsonable message as the Logfire records that carry it."""
    body = json.dumps(message, separators=(",", ":")).encode()
    chunks = _chunk_b64(body)
    total = len(chunks)
    records = []
    for index, chunk in enumerate(chunks):
        attrs: dict[str, Any] = {
            "cp.hist.name": name,
            "cp.hist.seq": seq,
            "cp.hist.chunk": f"{index}/{total}",
            "cp.hist.payload": chunk,
        }
        if scope_key:
            attrs["cp.hist.scope_key"] = scope_key
        if project_name:
            attrs["cp.project.name"] = project_name
        if remote:
            attrs["cp.project.remote"] = remote
        if branch:
            attrs["cp.project.branch"] = branch
        records.append(attrs)
    return records


def decode_message_rows(rows: Iterable[dict[str, Any]]) -> list[Any]:
    """Rebuild the messages list from normalized rows.

    Each row is ``{"attributes": {...}, "timestamp": float | None}``. Rows
    sharing a ``seq`` are joined along their chunk index; when a seq appears
    more than once (re-synced history), the latest timestamp wins.
    """
    by_seq: dict[int, tuple[float, dict[int, str]]] = {}
    for row in rows:
        attrs = row.get("attributes") or {}
        name = attrs.get("cp.hist.name")
        if name is None or "cp.hist.seq" not in attrs:
            continue
        seq = int(attrs["cp.hist.seq"])
        chunk_index, _, _ = str(attrs.get("cp.hist.chunk", "0/1")).partition("/")
        timestamp = float(row.get("timestamp") or 0.0)
        seen_ts, chunks = by_seq.get(seq, (-1.0, {}))
        if timestamp >= seen_ts:
            # Later generations of a re-synced seq win, regardless of row order.
            chunks[int(chunk_index)] = attrs.get("cp.hist.payload", "")
        by_seq[seq] = (max(seen_ts, timestamp), chunks)

    messages: list[Any] = []
    for seq in sorted(by_seq):
        chunks = by_seq[seq][1]
        blob = "".join(chunks[i] for i in range(len(chunks)))
        raw = gzip.decompress(base64.b64decode(blob))
        messages.append(json.loads(raw))
    return messages


def state_path() -> Path:
    """Sync bookkeeping lives user-side, never next to plugin code (the trust
    hash covers the plugin directory; writing runtime state there would
    self-tamper and demand re-acceptance on every boot)."""
    return Path.home() / ".code_puppy" / "logfire_session_sync.json"


def load_state() -> dict[str, Any]:
    try:
        state = json.loads(state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"version": STATE_VERSION, "sessions": {}}
    if not isinstance(state.get("sessions"), dict):
        return {"version": STATE_VERSION, "sessions": {}}
    return state


def save_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def synced_entry(fingerprints: list[str]) -> dict[str, Any]:
    return {
        "fingerprints": fingerprints,
        "message_count": len(fingerprints),
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
