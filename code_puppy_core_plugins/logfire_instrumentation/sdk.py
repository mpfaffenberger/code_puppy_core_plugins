"""Discovery of the optional ``logfire`` SDK, shared by every Logfire plugin."""

from __future__ import annotations

import importlib.util
import sys

MISSING_SDK_HINT = (
    "The optional 'logfire' SDK is not installed. Install the plugin extra and "
    "restart Code Puppy, e.g. "
    "uv tool install code-puppy --with 'code-puppy-core-plugins[logfire]'"
)


def module_exists(name: str) -> bool:
    """Return whether ``name`` can be imported, without importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def sdk_available() -> bool:
    """Return whether the ``logfire`` SDK is loaded or importable."""
    return sys.modules.get("logfire") is not None or module_exists("logfire")
