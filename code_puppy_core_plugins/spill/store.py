"""Private local storage for spilled tool-result text."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import tempfile
import uuid
from pathlib import Path

_default_root: Path | None = None
_process_session_id: str | None = None
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def private_root(configured_root: str | None = None) -> Path:
    """Return the configured root or a lazy private per-process temp root."""
    global _default_root
    if configured_root:
        root = Path(configured_root).expanduser().resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return root
    if _default_root is None:
        _default_root = Path(tempfile.mkdtemp(prefix="code-puppy-spill-"))
    return _default_root


def current_session_id() -> str:
    """Return the active agent session id, with a stable process fallback."""
    global _process_session_id
    try:
        from code_puppy.messaging import get_session_context

        session_id = get_session_context()
        if session_id:
            return str(session_id)
    except Exception:
        pass
    if _process_session_id is None:
        _process_session_id = uuid.uuid4().hex
    return _process_session_id


def session_dir(root: Path, session_id: str) -> Path:
    """Create and return a private, hashed session directory under ``root``."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    directory = root / f"session-{digest}"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def safe_filename(tool_name: str) -> str:
    """Convert an untrusted tool name into one harmless filename segment."""
    raw = f"{tool_name}.txt".replace("\x00", "_")
    safe = _UNSAFE_NAME.sub("_", raw.replace("/", "_").replace("\\", "_"))
    while ".." in safe:
        safe = safe.replace("..", "_")
    safe = safe.strip(".")
    return safe or "tool.txt"


def save_text(content: str, tool_name: str, configured_root: str | None = None) -> Path:
    """Persist ``content`` verbatim with exclusive owner-only creation."""
    root = private_root(configured_root)
    directory = session_dir(root, current_session_id())
    path = directory / f"{secrets.token_hex(6)}-{safe_filename(tool_name)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as spill_file:
            descriptor = -1
            spill_file.write(content)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return path


def _reset_state() -> None:
    """Forget lazy process state; test cleanup remains the caller's job."""
    global _default_root, _process_session_id
    _default_root = None
    _process_session_id = None


__all__ = [
    "_reset_state",
    "current_session_id",
    "private_root",
    "safe_filename",
    "save_text",
    "session_dir",
]
