"""Per-session working-directory scope and ``/cd`` parsing for ACP chats."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional


@contextmanager
def working_directory(cwd: Optional[str]) -> Iterator[None]:
    """Run the block with ``cwd`` as the working directory when it is set."""
    if not cwd:
        yield
        return
    from code_puppy.tools.common import reset_working_directory, set_working_directory

    token = set_working_directory(cwd)
    try:
        yield
    finally:
        reset_working_directory(token)


def cd_argument(text: str) -> Optional[str]:
    """Return the ``/cd`` path argument, ``""`` for bare ``/cd``, or ``None``."""
    parts = text.strip().split(None, 1)
    if not parts or parts[0] != "/cd":
        return None
    if len(parts) == 1:
        return ""
    return parts[1].strip().strip("\"'")
