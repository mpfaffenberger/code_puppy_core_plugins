"""Bridge to Code Puppy cores that still ship their own Logfire wiring.

Those cores configure Logfire themselves from ``code_puppy.observability``
before the ``startup`` hook fires, and read the write token only from the
environment. This module never imports that legacy module: it only finds it,
or looks it up once core has imported it.

Delete this module once the minimum supported core no longer ships
``code_puppy.observability``.
"""

from __future__ import annotations

import os
import sys
from types import ModuleType

from .sdk import module_exists

MODULE_NAME = "code_puppy.observability"
TOKEN_ENV = "LOGFIRE_TOKEN"


def present() -> bool:
    """Return whether the installed core ships its own Logfire wiring."""
    return module_exists(MODULE_NAME)


def loaded() -> ModuleType | None:
    """Return the legacy module if core already imported it."""
    return sys.modules.get(MODULE_NAME)


def export_token(token: str | None, *, overwrite: bool) -> None:
    """Expose ``token`` to a legacy core, the only reader that needs the env."""
    if not token or not present():
        return
    if overwrite or TOKEN_ENV not in os.environ:
        os.environ[TOKEN_ENV] = token
