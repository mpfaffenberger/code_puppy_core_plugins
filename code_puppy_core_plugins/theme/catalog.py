"""Pure, read-only theme catalog for ACP clients.

Depends on: themes.py :: MENU, CURATED_THEMES, DEFAULT
Never applies palettes, emits OSC sequences, or writes config.
"""

from __future__ import annotations

import re
from typing import Any

from code_puppy.config import get_value

from .themes import CURATED_THEMES, DEFAULT, MENU

# Called by: register_callbacks.py :: _apply_theme(), _active_terminal_palette(),
# _apply_default_theme_on_first_run()
ACTIVE_THEME_CONFIG_KEY = "theme_active_theme"
SCHEMA_VERSION = 1

_AA_MIN_CONTRAST = 4.5
_SURFACE_BLEND = 0.08
_DARK_LUMINANCE_CUTOFF = 0.179
_ADJUST_STEPS = 20  # 5% blend increments toward white or black
_WHITE = (255, 255, 255)
_BLACK = (0, 0, 0)
_HEX_RE = re.compile(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")


def appearance_for(bg_hex: str) -> str:
    return "dark" if _is_dark(_rgb(bg_hex)) else "light"


def semantic_roles(palette: dict[str, Any]) -> dict[str, str]:
    """Map a bg/fg/ansi palette to semantic role colors.

    Non-background roles are blended toward white (dark bg) or black (light bg)
    in 5% steps until they reach WCAG AA (4.5:1) against the background. The
    input palette is never mutated.
    """
    bg = _rgb(palette["bg"])
    fg = _rgb(palette["fg"])
    ansi = [_rgb(color) for color in palette["ansi"]]
    adjust_target = _WHITE if _is_dark(bg) else _BLACK
    adjustable = {
        "text": fg,
        "mutedText": ansi[8],
        "accent": ansi[4],
        "success": ansi[2],
        "error": ansi[1],
        "warning": ansi[3],
        "running": ansi[6],
        "diffAdd": ansi[2],
        "diffRemove": ansi[1],
    }
    roles = {
        "background": _hex(bg),
        "surface": _hex(_blend(bg, fg, _SURFACE_BLEND)),
    }
    for role, color in adjustable.items():
        roles[role] = _hex(_meet_aa(color, bg, adjust_target))
    return roles


def build_catalog() -> list[dict[str, Any]]:
    """Return fresh wire dicts: curated themes in menu order, then default.

    Every call builds new containers, so callers cannot mutate shared state.
    """
    entries = [_curated_entry(theme_id) for theme_id in _curated_ids()]
    entries.append(_default_entry())
    return entries


def active_theme_id(raw: object) -> str | None:
    if isinstance(raw, str) and (raw == "default" or raw in _curated_ids()):
        return raw
    return None


def read_active_theme_id() -> str | None:
    try:
        return active_theme_id(get_value(ACTIVE_THEME_CONFIG_KEY))
    except Exception:
        return None


def _curated_ids() -> list[str]:
    # Resolved per call so a theme added to MENU/CURATED_THEMES reaches ACP without edits here.
    return [name for name, _ in MENU if name in CURATED_THEMES]


def _curated_entry(theme_id: str) -> dict[str, Any]:
    theme = CURATED_THEMES[theme_id]
    palette = _normalised_palette(theme["terminal_palette"])
    return {
        "id": theme_id,
        "label": theme["label"],
        "icon": theme["icon"],
        "blurb": theme["blurb"],
        "appearance": appearance_for(palette["bg"]),
        "palette": palette,
        "roles": semantic_roles(palette),
    }


def _default_entry() -> dict[str, Any]:
    return {
        "id": "default",
        "label": DEFAULT["label"],
        "icon": DEFAULT["icon"],
        "blurb": DEFAULT["blurb"],
        "appearance": None,
        "palette": None,
        "roles": None,
    }


def _normalised_palette(palette: dict[str, Any]) -> dict[str, Any]:
    return {
        "bg": _canonical(palette["bg"]),
        "fg": _canonical(palette["fg"]),
        "ansi": [_canonical(color) for color in palette["ansi"]],
    }


def _meet_aa(
    color: tuple[int, int, int], bg: tuple[int, int, int], target: tuple[int, int, int]
) -> tuple[int, int, int]:
    if _contrast(color, bg) >= _AA_MIN_CONTRAST:
        return color
    for step in range(1, _ADJUST_STEPS + 1):
        candidate = _blend(color, target, step / _ADJUST_STEPS)
        if _contrast(candidate, bg) >= _AA_MIN_CONTRAST:
            return candidate
    # Only reachable if the target fails AA, which cannot happen: white clears
    # 4.5:1 on any bg below the dark cutoff, and black on any bg at or above it.
    return target


def _is_dark(rgb: tuple[int, int, int]) -> bool:
    return _luminance(rgb) < _DARK_LUMINANCE_CUTOFF


def _rgb(color: str) -> tuple[int, int, int]:
    match = _HEX_RE.fullmatch(color)
    if match is None:
        raise ValueError(f"not a hex color: {color!r}")
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(digit * 2 for digit in digits)
    return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{channel:02x}" for channel in rgb)


def _canonical(color: str) -> str:
    return _hex(_rgb(color))


def _blend(
    start: tuple[int, int, int], end: tuple[int, int, int], amount: float
) -> tuple[int, int, int]:
    r, g, b = (round(s + (e - s) * amount) for s, e in zip(start, end))
    return (r, g, b)


def _linearise(value: float) -> float:
    if value <= 0.03928:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (_linearise(channel / 255) for channel in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)
