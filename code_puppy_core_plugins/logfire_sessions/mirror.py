"""Write path: mirror a saved session envelope into Logfire records.

Called from the ``post_autosave`` hook. Local JSON stays canonical; Logfire is
a roaming/backup layer. Everything here raises on failure -- the hook wrapper
owns the fail-soft policy.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import sync

_CONFIGURED = False


def _configure_once() -> None:
    """Point the logfire SDK at the onboarded project's write token."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    import logfire

    from ..logfire_oauth.oauth import load_project_credentials

    credentials = load_project_credentials()
    if credentials is None:
        raise RuntimeError("no Logfire project credentials; run /logfire onboard")
    logfire.configure(
        service_name="code-puppy",
        token=credentials.token,
        send_to_logfire="always",
        console=False,
    )
    _CONFIGURED = True


@lru_cache(maxsize=8)
def _git(args: tuple[str, ...], cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ("git", *args),
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def workspace_metadata() -> dict[str, str | None]:
    """Best-effort workspace identity: resolved cwd, git remote, branch.

    ``remote`` is the cross-machine join key (paths differ between machines);
    ``scope_key`` is the machine-local grouping used by session resume.
    """
    cwd = str(Path.cwd())
    return {
        "scope_key": cwd,
        "project_name": Path(cwd).name or None,
        "remote": _git(("remote", "get-url", "origin"), cwd),
        "branch": _git(("rev-parse", "--abbrev-ref", "HEAD"), cwd),
    }


def mirror_session(metadata: Any) -> tuple[int, int]:
    """Emit not-yet-synced messages of a saved session. Returns (emitted, total)."""
    from code_puppy.session_storage import read_envelope_file

    envelope = read_envelope_file(Path(metadata.json_path))
    messages = envelope.get("messages") or []
    fps = [sync.fingerprint(message) for message in messages]

    key = f"{metadata.scope_key or ''}/{metadata.session_name}"
    state = sync.load_state()
    previous = (state["sessions"].get(key) or {}).get("fingerprints")
    start = sync.plan_sync(previous, fps)
    if start >= len(messages):
        return 0, len(messages)

    meta = workspace_metadata()
    _configure_once()
    import logfire

    emitted = 0
    for seq in range(start, len(messages)):
        for attrs in sync.encode_message_records(
            name=metadata.session_name,
            seq=seq,
            message=messages[seq],
            scope_key=meta["scope_key"],
            project_name=meta["project_name"],
            remote=meta["remote"],
            branch=meta["branch"],
        ):
            logfire.info("cp.hist.message", **attrs)
            emitted += 1
    logfire.force_flush()

    state["sessions"][key] = sync.synced_entry(fps)
    sync.save_state(state)
    return emitted, len(messages)
