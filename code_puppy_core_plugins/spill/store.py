"""Private local storage for spilled tool-result text."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import tempfile
import threading
import uuid
from pathlib import Path

_default_root: Path | None = None
_process_session_id: str | None = None
_state_lock = threading.Lock()
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def private_root(configured_root: str | None = None) -> Path:
    """Return the configured root or a lazy private per-process temp root."""
    global _default_root
    if configured_root:
        root = Path(configured_root).expanduser().resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return root
    with _state_lock:
        if _default_root is None:
            _default_root = Path(tempfile.mkdtemp(prefix="code-puppy-spill-"))
        return _default_root


def _process_fallback_session_id() -> str:
    global _process_session_id
    with _state_lock:
        if _process_session_id is None:
            _process_session_id = uuid.uuid4().hex
        return _process_session_id


def current_session_id() -> str:
    """Return an async-safe session scope, with a stable process fallback."""
    try:
        from code_puppy.tools.subagent_context import get_conversation_root_id

        conversation_id = get_conversation_root_id()
        if conversation_id:
            return str(conversation_id)
    except Exception:
        pass

    # MessageBus session state is process-global and can be overwritten by any
    # concurrent agent task. If no async-safe conversation root is available,
    # prefer the honest process fallback over confidently misattributing a spill.
    return _process_fallback_session_id()


def _session_name(session_id: str) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return f"session-{digest}"


def session_dir(root: Path, session_id: str) -> Path:
    """Create a private real directory under ``root``, rejecting symlinks."""
    directory = root / _session_name(session_id)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = directory.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OSError(f"Unsafe spill session directory: {directory}")
    if os.chmod in os.supports_follow_symlinks:
        os.chmod(directory, 0o700, follow_symlinks=False)
    else:
        # Platforms without no-follow chmod also lack the directory-fd path
        # below. Re-check immediately before the best available fallback.
        if directory.is_symlink():
            raise OSError(f"Unsafe spill session directory: {directory}")
        os.chmod(directory, 0o700)
    return directory


def _open_session_dir(root: Path, session_id: str) -> tuple[Path, int | None]:
    """Open a session directory without following a pre-planted symlink."""
    supports_safe_open = (
        os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "fchmod")
    )
    if not supports_safe_open:
        return session_dir(root, session_id), None

    name = _session_name(session_id)
    root_flags = os.O_RDONLY | os.O_DIRECTORY
    root_fd = os.open(root, root_flags)
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_fd = os.open(name, flags, dir_fd=root_fd)
    finally:
        os.close(root_fd)

    try:
        os.fchmod(directory_fd, 0o700)
    except Exception:
        os.close(directory_fd)
        raise
    return root / name, directory_fd


def safe_filename(tool_name: str) -> str:
    """Convert an untrusted tool name into one harmless filename segment."""
    raw = f"{tool_name}.txt".replace("\x00", "_")
    safe = _UNSAFE_NAME.sub("_", raw.replace("/", "_").replace("\\", "_"))
    while ".." in safe:
        safe = safe.replace("..", "_")
    safe = safe.strip(".")
    return safe or "tool.txt"


def save_text(
    content: str,
    tool_name: str,
    configured_root: str | None = None,
    session_id: str | None = None,
) -> Path:
    """Persist ``content`` verbatim with exclusive owner-only creation."""
    root = private_root(configured_root)
    directory, directory_fd = _open_session_dir(
        root, session_id or current_session_id()
    )
    filename = f"{secrets.token_hex(6)}-{safe_filename(tool_name)}"
    path = directory / filename
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = -1
    try:
        if directory_fd is None:
            descriptor = os.open(path, flags, 0o600)
        else:
            descriptor = os.open(filename, flags, 0o600, dir_fd=directory_fd)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as spill_file:
            descriptor = -1
            spill_file.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_fd is not None:
            os.close(directory_fd)
    return path


def _reset_state() -> None:
    """Forget lazy process state; test cleanup remains the caller's job."""
    global _default_root, _process_session_id
    with _state_lock:
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
