"""Contract tests for the pure ACP theme catalog."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_puppy import config
from code_puppy_core_plugins.theme import content_styles, osc_palette, rich_themes
from code_puppy_core_plugins.theme.catalog import (
    ACTIVE_THEME_CONFIG_KEY,
    SCHEMA_VERSION,
    active_theme_id,
    appearance_for,
    build_catalog,
    read_active_theme_id,
    semantic_roles,
)
from code_puppy_core_plugins.theme.themes import CURATED_THEMES, DEFAULT

REPO_ROOT = Path(__file__).resolve().parents[1]
HEX_RE = re.compile(r"#[0-9a-f]{6}")
CURATED_IDS = [
    "ocean",
    "forest",
    "sunset",
    "vaporwave",
    "bubblegum-pink",
    "purple-puppy",
    "catppuccin-mocha",
    "catppuccin-latte",
    "tokyo-night",
    "green-screen",
    "deep-black",
    "solarized-light",
    "github-light",
    "rose-pine-dawn",
]
EXPECTED_IDS = [*CURATED_IDS, "default"]
EXPECTED_APPEARANCE = {
    **{theme_id: "dark" for theme_id in CURATED_IDS},
    "catppuccin-latte": "light",
    "solarized-light": "light",
    "github-light": "light",
    "rose-pine-dawn": "light",
}
ROLE_KEYS = {
    "background",
    "surface",
    "text",
    "mutedText",
    "accent",
    "success",
    "error",
    "warning",
    "running",
    "diffAdd",
    "diffRemove",
}


def _entry(theme_id: str) -> dict:
    return next(e for e in build_catalog() if e["id"] == theme_id)


# Independent WCAG 2.2 implementation (test-side oracle, not the module's code).
def _channel_linear(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _wcag_luminance(color: str) -> float:
    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return (
        0.2126 * _channel_linear(r)
        + 0.7152 * _channel_linear(g)
        + 0.0722 * _channel_linear(b)
    )


def _wcag_contrast(first: str, second: str) -> float:
    lighter, darker = sorted(
        (_wcag_luminance(first), _wcag_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


# Test-side reference for the semantic-role adjustment rule (not catalog.py's code).
WHITE_HEX = "#ffffff"
BLACK_HEX = "#000000"
DARK_LOW_CONTRAST = [
    "#202020",
    "#303030",
    "#404040",
    "#505050",
    "#1a237e",
    "#283593",
    "#3949ab",
    "#0d2a4a",
    "#2e2e2e",
    "#363636",
    "#3c3c3c",
    "#424242",
    "#4a2a2a",
    "#2a4a2a",
    "#2a2a4a",
    "#4a4a2a",
]
LIGHT_LOW_CONTRAST = [
    "#dddddd",
    "#ffff99",
    "#e0e0ff",
    "#ffe0e0",
    "#eeeeee",
    "#f0f0c0",
    "#e6ffe6",
    "#fff0d0",
    "#d0e8ff",
    "#f5f5f5",
    "#ffffcc",
    "#e8e0ff",
    "#ffd6d6",
    "#dff5df",
    "#fdf0c8",
    "#cfe8ff",
]


def _ref_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))


def _ref_blend(hex_a: str, hex_b: str, amount: float) -> str:
    channels = [
        round(start + (end - start) * amount)
        for start, end in zip(_ref_rgb(hex_a), _ref_rgb(hex_b))
    ]
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def _ref_first_step(color: str, bg: str, target: str) -> int:
    for step in range(1, 21):
        if _wcag_contrast(_ref_blend(color, target, step / 20), bg) >= 4.5:
            return step
    raise AssertionError(f"{color} never reaches AA toward {target}")


def _ref_adjusted(color: str, bg: str, target: str) -> str:
    if _wcag_contrast(color, bg) >= 4.5:
        return color.lower()
    return _ref_blend(color, target, _ref_first_step(color, bg, target) / 20)


def _palette(bg: str, fg: str, ansi: list[str]) -> dict:
    return {"bg": bg, "fg": fg, "ansi": list(ansi)}


def _role_inputs(palette: dict) -> dict[str, str]:
    ansi = palette["ansi"]
    return {
        "text": palette["fg"],
        "mutedText": ansi[8],
        "accent": ansi[4],
        "success": ansi[2],
        "error": ansi[1],
        "warning": ansi[3],
        "running": ansi[6],
        "diffAdd": ansi[2],
        "diffRemove": ansi[1],
    }


def _gray(value: int) -> str:
    return f"#{value:02x}{value:02x}{value:02x}"


def _assert_first_step_adjustment(
    bg: str, low_contrast: list[str], target: str, fg: str
):
    palette = _palette(bg, fg, low_contrast)
    roles = semantic_roles(palette)
    for role, color in _role_inputs(palette).items():
        assert _wcag_contrast(color, bg) < 4.5, (role, "input must fail AA")
        step = _ref_first_step(color, bg, target)
        assert roles[role] == _ref_blend(color, target, step / 20), role
        assert roles[role] != color, role
        assert _wcag_contrast(roles[role], bg) >= 4.5, role
        previous = color if step == 1 else _ref_blend(color, target, (step - 1) / 20)
        assert _wcag_contrast(previous, bg) < 4.5, (role, "step is not first")


class TestCatalogShape:
    def test_ids_follow_menu_order_with_default_last_and_no_surprise(self):
        assert [e["id"] for e in build_catalog()] == EXPECTED_IDS

    @pytest.mark.parametrize("theme_id", CURATED_IDS)
    def test_curated_entry_has_documented_fields(self, theme_id):
        entry = _entry(theme_id)
        theme = CURATED_THEMES[theme_id]
        assert set(entry) == {
            "id",
            "label",
            "icon",
            "blurb",
            "appearance",
            "palette",
            "roles",
        }
        assert (entry["label"], entry["icon"], entry["blurb"]) == (
            theme["label"],
            theme["icon"],
            theme["blurb"],
        )
        assert set(entry["palette"]) == {"bg", "fg", "ansi"}
        assert len(entry["palette"]["ansi"]) == 16
        colors = [entry["palette"]["bg"], entry["palette"]["fg"]]
        colors += entry["palette"]["ansi"]
        assert all(HEX_RE.fullmatch(color) for color in colors)
        assert set(entry["roles"]) == ROLE_KEYS
        assert all(HEX_RE.fullmatch(color) for color in entry["roles"].values())

    def test_default_entry_is_placeholder_without_palette(self):
        entry = _entry("default")
        assert entry == {
            "id": "default",
            "label": DEFAULT["label"],
            "icon": DEFAULT["icon"],
            "blurb": DEFAULT["blurb"],
            "appearance": None,
            "palette": None,
            "roles": None,
        }


class TestSemanticRoles:
    @pytest.mark.parametrize("theme_id", CURATED_IDS)
    def test_non_surface_roles_meet_aa_against_background(self, theme_id):
        entry = _entry(theme_id)
        bg = entry["roles"]["background"]
        for role, color in entry["roles"].items():
            if role in {"background", "surface"}:
                continue
            assert _wcag_contrast(color, bg) >= 4.5, (theme_id, role)

    @pytest.mark.parametrize("theme_id", CURATED_IDS)
    def test_mapping_is_deterministic_and_leaves_input_unmutated(self, theme_id):
        palette = copy.deepcopy(CURATED_THEMES[theme_id]["terminal_palette"])
        snapshot = copy.deepcopy(palette)
        first = semantic_roles(palette)
        assert semantic_roles(palette) == first
        assert palette == snapshot

    def test_short_and_uppercase_hex_is_normalised(self):
        palette = {"bg": "#000", "fg": "#FFF", "ansi": ["#F00"] * 16}
        roles = semantic_roles(palette)
        assert roles["background"] == "#000000"
        assert roles["text"] == "#ffffff"
        assert all(HEX_RE.fullmatch(color) for color in roles.values())

    def test_surface_is_eight_percent_blend_toward_foreground(self):
        roles = semantic_roles(
            {"bg": "#000000", "fg": "#ffffff", "ansi": ["#000000"] * 16}
        )
        assert roles["surface"] == "#141414"

    @pytest.mark.parametrize("bg", ["#101010", "#0a1929"])
    def test_dark_bg_low_contrast_roles_take_first_white_step_meeting_aa(self, bg):
        _assert_first_step_adjustment(bg, DARK_LOW_CONTRAST, WHITE_HEX, "#2e2e2e")

    @pytest.mark.parametrize("bg", ["#ffffff", "#fdf6e3"])
    def test_light_bg_low_contrast_roles_take_first_black_step_meeting_aa(self, bg):
        _assert_first_step_adjustment(bg, LIGHT_LOW_CONTRAST, BLACK_HEX, "#f5f5f5")

    @pytest.mark.parametrize(
        ("bg", "color", "expected"),
        [
            ("#101010", "#E0E0FF", "#e0e0ff"),
            ("#ffffff", "#1A1A8A", "#1a1a8a"),
        ],
    )
    def test_role_already_meeting_aa_is_returned_unchanged(self, bg, color, expected):
        palette = _palette(bg, color, [color] * 16)
        roles = semantic_roles(palette)
        for role in _role_inputs(palette):
            assert roles[role] == expected, role

    @pytest.mark.parametrize(
        ("bg", "fg"),
        [("#101010", "#e6e6e6"), ("#fdf6e3", "#657b83"), ("#0a1929", "#ffffff")],
    )
    def test_surface_is_eight_percent_reference_blend(self, bg, fg):
        roles = semantic_roles(_palette(bg, fg, ["#808080"] * 16))
        assert roles["surface"] == _ref_blend(bg, fg, 0.08)

    def test_appearance_boundary_at_luminance_cutoff_drives_adjust_direction(self):
        below, above = 0, 255  # luminance(0) < 0.179 <= luminance(255)
        while above - below > 1:
            middle = (below + above) // 2
            if _wcag_luminance(_gray(middle)) < 0.179:
                below = middle
            else:
                above = middle
        dark_bg, light_bg = _gray(below), _gray(above)
        assert _wcag_luminance(dark_bg) < 0.179 <= _wcag_luminance(light_bg)
        assert appearance_for(dark_bg) == "dark"
        assert appearance_for(light_bg) == "light"

        mid_gray = "#808080"
        dark_roles = semantic_roles(_palette(dark_bg, mid_gray, [mid_gray] * 16))
        light_roles = semantic_roles(_palette(light_bg, mid_gray, [mid_gray] * 16))
        for role in _role_inputs(_palette(mid_gray, mid_gray, [mid_gray] * 16)):
            assert dark_roles[role] == _ref_adjusted(mid_gray, dark_bg, WHITE_HEX)
            assert light_roles[role] == _ref_adjusted(mid_gray, light_bg, BLACK_HEX)
        assert all(channel > 0x80 for channel in _ref_rgb(dark_roles["mutedText"]))
        assert all(channel < 0x80 for channel in _ref_rgb(light_roles["mutedText"]))


class TestAppearance:
    @pytest.mark.parametrize(("theme_id", "expected"), EXPECTED_APPEARANCE.items())
    def test_entry_appearance_follows_background_luminance(self, theme_id, expected):
        assert _entry(theme_id)["appearance"] == expected

    @pytest.mark.parametrize(
        ("bg", "expected"),
        [
            ("#000000", "dark"),
            ("#0a1929", "dark"),
            ("#ffffff", "light"),
            ("#fdf6e3", "light"),
        ],
    )
    def test_appearance_for_classifies_background(self, bg, expected):
        assert appearance_for(bg) == expected


class TestBuildCatalogIsolation:
    def test_repeated_calls_are_equal_but_independent(self):
        first = build_catalog()
        second = build_catalog()
        json.dumps(first)
        assert first == second
        assert first is not second
        assert first[0]["palette"]["ansi"] is not second[0]["palette"]["ansi"]

        first[0]["palette"]["ansi"][0] = "#123456"
        first[0]["roles"]["text"] = "#123456"
        first.pop()
        assert build_catalog() == second


class TestNoSideEffects:
    # Subprocess stdout is not a TTY and no palette is saved, so this cannot detect TTY-only writes; the in-process test below does.
    @pytest.mark.parametrize("seed_config", [False, True])
    def test_import_and_reads_print_nothing_and_never_write_config(
        self, tmp_path, seed_config
    ):
        home = tmp_path / "home"
        home.mkdir()
        seeded = home / ".code_puppy" / "puppy.cfg"
        seed_bytes = b"[puppy]\ntheme_active_theme = ocean\n"
        if seed_config:
            seeded.parent.mkdir(parents=True)
            seeded.write_bytes(seed_bytes)

        env = dict(os.environ)
        env.update(
            HOME=str(home),
            USERPROFILE=str(home),
            XDG_CONFIG_HOME=str(home),
            PYTHONPATH=os.pathsep.join(
                filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")])
            ),
        )
        script = (
            "from code_puppy_core_plugins.theme.catalog import "
            "build_catalog, read_active_theme_id\n"
            "build_catalog()\n"
            "read_active_theme_id()\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            timeout=120,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == b""
        assert result.stderr == b""
        assert b"\x1b]" not in result.stdout + result.stderr
        found = sorted(home.rglob("puppy.cfg"))
        if seed_config:
            assert found == [seeded]
            assert seeded.read_bytes() == seed_bytes
        else:
            assert found == []

    def test_in_process_calls_print_nothing_and_never_write_config(self, capsys):
        path = config.CONFIG_FILE
        palette = CURATED_THEMES["ocean"]["terminal_palette"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[puppy]\n"
            f"osc_palette_json = {json.dumps(palette)}\n"
            "theme_active_theme = ocean\n",
            encoding="utf-8",
        )
        before = path.read_bytes()
        targets = [
            (osc_palette, "apply_palette"),
            (osc_palette, "reset_palette"),
            (osc_palette, "_emit"),
            (osc_palette, "set_config_value"),
            (rich_themes, "set_config_value"),
            (content_styles, "set_config_value"),
            # register_callbacks imports set_config_value inside functions, so this covers it.
            (config, "set_config_value"),
        ]
        with ExitStack() as stack:
            recorders = [
                stack.enter_context(
                    patch.object(module, name, new=MagicMock(), create=False)
                )
                for module, name in targets
            ]
            build_catalog()
            semantic_roles(palette)
            active_theme_id("ocean")
            assert read_active_theme_id() == "ocean"
            for recorder in recorders:
                recorder.assert_not_called()
        out, err = capsys.readouterr()
        assert (out, err) == ("", "")
        assert path.read_bytes() == before


class TestActiveThemeId:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, None),
            ("ocean", "ocean"),
            ("catppuccin-latte", "catppuccin-latte"),
            ("default", "default"),
            ("legacy-custom", None),
            ("surprise", None),
            ("bogus", None),
            ("mocha", None),
        ],
    )
    def test_read_active_theme_id_maps_stored_value(self, raw, expected):
        with patch(
            "code_puppy_core_plugins.theme.catalog.get_value", return_value=raw
        ) as get_value:
            assert read_active_theme_id() == expected
        get_value.assert_called_once_with(ACTIVE_THEME_CONFIG_KEY)

    def test_read_active_theme_id_swallows_config_errors(self):
        with patch(
            "code_puppy_core_plugins.theme.catalog.get_value",
            side_effect=RuntimeError("config unreadable"),
        ):
            assert read_active_theme_id() is None

    @pytest.mark.parametrize("raw", [None, 123, ["ocean"], b"ocean", "Ocean"])
    def test_active_theme_id_rejects_non_catalog_values(self, raw):
        assert active_theme_id(raw) is None

    def test_constants_are_wire_contract(self):
        assert ACTIVE_THEME_CONFIG_KEY == "theme_active_theme"
        assert SCHEMA_VERSION == 1
