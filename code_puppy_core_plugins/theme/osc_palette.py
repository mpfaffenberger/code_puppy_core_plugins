"""Terminal palette swap: Code Puppy persistence over termflow's OSC engine.

The escape-sequence machinery (OSC 4/10/11 builders, palette
application, reset) lives in ``termflow.themes.osc``. This module adds
what is Code Puppy's business alone:

* persisting the applied palette to config so the next session replays it
* re-firing the persisted palette on plugin load
* an atexit restore so the terminal never stays stuck in a theme

Emission is routed through the local :func:`_emit` (which tests patch)
via a callback stream handed to termflow.
"""

from __future__ import annotations

import atexit
import json
import sys
from typing import Optional

from termflow.themes import osc as tf_osc
from termflow.themes.osc import BEL, ESC, _osc  # noqa: F401  (re-exported API)

from code_puppy.config import get_value, set_config_value

_CONFIG_KEY = "osc_palette_json"
_atexit_registered = False


# --- Emission ---------------------------------------------------------------
def _emit(seq: str) -> None:
    """Write an escape sequence to stdout, ignoring failures (closed tty etc.)."""
    try:
        sys.stdout.write(seq)
        sys.stdout.flush()
    except Exception:
        pass


class _EmitStream:
    """File-like shim routing termflow's writes through :func:`_emit`.

    Looks up the module global at call time so test patches of ``_emit``
    keep working.
    """

    def write(self, seq: str) -> int:
        _emit(seq)
        return len(seq)

    def flush(self) -> None:
        pass


def set_bg(color: str) -> None:
    tf_osc.set_bg(color, _EmitStream())


def set_fg(color: str) -> None:
    tf_osc.set_fg(color, _EmitStream())


def set_ansi_slot(slot: int, color: str) -> None:
    tf_osc.set_ansi_slot(slot, color, _EmitStream())


# --- High-level API ---------------------------------------------------------
def apply_palette(
    palette: dict, persist: bool = True, register_reset: bool = True
) -> None:
    """Apply a palette dict to the live terminal.

    `persist=True` writes the palette to config so the next Code Puppy
    session can replay it. `register_reset=True` ensures we always
    restore the terminal at process exit.
    """
    if not isinstance(palette, dict):
        return

    tf_osc.apply_palette(palette, output=_EmitStream(), register_reset=False)

    if persist:
        try:
            set_config_value(_CONFIG_KEY, json.dumps(palette))
        except Exception:
            pass

    if register_reset:
        _ensure_atexit_registered()


def reset_palette(persist: bool = True) -> None:
    """Restore the terminal's original bg/fg/ANSI palette."""
    tf_osc.reset_palette(output=_EmitStream())
    if persist:
        try:
            set_config_value(_CONFIG_KEY, "")
        except Exception:
            pass


def get_saved_palette() -> Optional[dict]:
    """Read the persisted palette (or None if nothing saved)."""
    raw = get_value(_CONFIG_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def reapply_from_config() -> None:
    """On plugin load, re-fire any persisted palette into the terminal."""
    palette = get_saved_palette()
    if palette:
        apply_palette(palette, persist=False)


# --- Cleanup ----------------------------------------------------------------
def _at_exit_reset() -> None:
    """Best-effort terminal restore on Python exit.

    We DON'T touch persisted config here — if the user wants the palette
    next session, they get it; this just makes sure the live terminal
    doesn't stay stuck in a weird color after Code Puppy dies.
    """
    try:
        reset_palette(persist=False)
    except Exception:
        pass


def _ensure_atexit_registered() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    try:
        atexit.register(_at_exit_reset)
        _atexit_registered = True
    except Exception:
        pass
