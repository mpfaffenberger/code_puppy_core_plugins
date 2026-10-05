"""Platform selection behind the stable computer_* tool contract.

Native frameworks are lazy: importing the plugin never captures the desktop,
initializes COM, or prompts for accessibility permissions.
"""

from __future__ import annotations

import sys

from .backend_types import ComputerUseError
from .macos_backend import MacOSBackend

__all__ = ["ComputerUseError", "MacOSBackend", "backend", "create_backend"]


def create_backend(platform: str | None = None):
    platform = platform or sys.platform
    if platform == "win32":
        from .windows_runtime import WindowsRuntime

        return WindowsRuntime()
    # Keep the importable macOS backend for existing callers and non-native unit
    # tests. Registration separately gates tools to the two supported platforms.
    return MacOSBackend()


backend = create_backend()
