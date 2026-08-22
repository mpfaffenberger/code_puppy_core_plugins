"""Bundled terminal palettes, served from termflow.

The palette data graduated into ``termflow.themes`` (the canonical
copy); this module re-exposes each palette in the plugin's historical
dict shape (``{"bg", "fg", "ansi"}``) so themes.py and downstream
consumers keep working unchanged.
"""

from __future__ import annotations

from termflow.themes import get_palette


def _palette_dict(name: str) -> dict:
    palette = get_palette(name)
    if palette is None:  # pragma: no cover - termflow always bundles these
        raise KeyError(f"termflow is missing bundled palette {name!r}")
    return palette.to_dict()


CATPPUCCIN_MOCHA = _palette_dict("catppuccin_mocha")
CATPPUCCIN_LATTE = _palette_dict("catppuccin_latte")
TOKYO_NIGHT = _palette_dict("tokyo_night")
GREEN_SCREEN = _palette_dict("green_screen")
DEEP_BLACK = _palette_dict("deep_black")
SOLARIZED_LIGHT = _palette_dict("solarized_light")
GITHUB_LIGHT = _palette_dict("github_light")
ROSE_PINE_DAWN = _palette_dict("rose_pine_dawn")
OCEAN = _palette_dict("ocean")
FOREST = _palette_dict("forest")
SUNSET = _palette_dict("sunset")
VAPORWAVE = _palette_dict("vaporwave")
PURPLE_PUPPY = _palette_dict("purple_puppy")
BUBBLEGUM_PINK = _palette_dict("bubblegum_pink")
