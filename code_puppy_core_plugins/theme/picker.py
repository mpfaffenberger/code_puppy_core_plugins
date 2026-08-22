"""Interactive theme picker TUI, built on termflow's MenuBuilder.

The left column lists the curated themes (paged); the right pane shows a
live preview with banners, content text styles, the inline-markup remap,
and the terminal palette swatches. Rendering the preview still goes
through Rich (it is the best ANSI paintbrush we have) -- the menu chrome
and event loop are pure termflow.
"""

from __future__ import annotations

import asyncio
import io
import random
from typing import Optional

from rich.console import Console
from termflow.tui import MenuBuilder, MenuItem

from code_puppy.command_line.colors_menu import (
    BANNER_DISPLAY_INFO,
    BANNER_SAMPLE_CONTENT,
)

from .themes import (
    MENU,
    color_remap_for,
    colors_for,
    content_styles_for,
    terminal_palette_for,
)

THEMES_PER_PAGE = 5
LIST_WIDTH = 40
PREVIEW_WIDTH = 70

# A few representative banners to show in the preview pane (keeps it readable).
PREVIEW_BANNERS = [
    "thinking",
    "agent_response",
    "shell_command",
    "read_file",
    "edit_file",
    "grep",
    "invoke_agent",
    "subagent_response",
]

# Sample lines to demonstrate body content styling.
CONTENT_SAMPLES = [
    ("info", "i  Heads up, this is an info message."),
    ("success", "v Success - task finished cleanly."),
    ("warning", "! Warning - proceeding with caution."),
    ("error", "x Error - something went wrong."),
    ("debug", ". debug trace (only shown if you ask)"),
]

# Inline-markup samples to demonstrate the Level 2 color remap.
# These mirror the kind of hardcoded tags scattered through the renderer.
INLINE_MARKUP_SAMPLES = [
    "[bold cyan]bold cyan headline[/bold cyan]",
    "[dim cyan]dim cyan detail[/dim cyan]",
    "[bold blue]bold blue label[/bold blue]",
    "[magenta]magenta accent[/magenta]",
]


def _render_preview(theme_name: str, surprise_seed: int) -> str:
    """Render a full-color ANSI preview of the theme via Rich.

    ``surprise_seed`` makes the "Surprise Me" preview stable while highlighted
    (otherwise it would re-roll on every redraw - dizzying).
    """
    buffer = io.StringIO()
    console = Console(
        file=buffer,
        force_terminal=True,
        width=PREVIEW_WIDTH,
        legacy_windows=False,
        color_system="truecolor",
        no_color=False,
        force_interactive=True,
    )

    rng = random.Random(surprise_seed) if theme_name == "surprise" else None
    banner_mapping = colors_for(theme_name, rng=rng)
    content_mapping = content_styles_for(theme_name, rng=rng)
    color_remap = color_remap_for(theme_name, rng=rng)
    theme = dict(MENU)[theme_name]

    # Apply the Level 2 remap to *this preview console only* so users see
    # exactly what the inline markup will look like.
    from . import rich_themes as rt

    if color_remap:
        rt._install_patch(console, color_remap)

    console.print("[bold]" + "=" * 60 + "[/bold]")
    console.print(
        f"[bold cyan] {theme['icon']} {theme['label']}[/bold cyan]  "
        f"[dim]- {theme['blurb']}[/dim]"
    )
    console.print("[bold]" + "=" * 60 + "[/bold]")
    console.print()

    # Banner samples
    for banner in PREVIEW_BANNERS:
        if banner not in banner_mapping:
            continue
        display, icon = BANNER_DISPLAY_INFO[banner]
        color = banner_mapping[banner]
        icon_str = f" {icon}" if icon else ""
        console.print(
            f"[bold white on {color}] {display} [/bold white on {color}]{icon_str}"
        )
        sample = BANNER_SAMPLE_CONTENT.get(banner, "")
        first_line = sample.split("\n", 1)[0]
        if first_line:
            console.print(f"    [dim]{first_line}[/dim]")
        console.print()

    # Content text samples (Level 1 theming)
    console.print("[bold]" + "-" * 60 + "[/bold]")
    console.print("[bold dim]content text styles:[/bold dim]")
    for key, text in CONTENT_SAMPLES:
        style = content_mapping[key]
        console.print(f"  [{style}]{text}[/]")

    # Inline markup samples (Level 2 - remapped colors)
    console.print("[bold dim]inline markup remap:[/bold dim]")
    for sample in INLINE_MARKUP_SAMPLES:
        console.print("  " + sample)
    console.print("[bold]" + "=" * 60 + "[/bold]")

    if theme_name == "surprise":
        console.print(
            "[dim italic]Every apply re-rolls a fresh random palette.[/dim italic]"
        )
    elif theme_name == "default":
        console.print(
            "[dim italic]Resets banners + content to Code Puppy defaults.[/dim italic]"
        )

    # Terminal-palette note (Level 3 - OSC sequences recolor the whole window)
    tp = terminal_palette_for(theme_name)
    if tp:
        bg = tp.get("bg", "?")
        fg = tp.get("fg", "?")
        ansi = tp.get("ansi") or []
        ansi_note = f" + {len(ansi)}-color ANSI palette" if ansi else ""

        # Preview the bg/fg combo without firing OSC live; arrow-key previews
        # must not flicker the terminal.
        console.print("[bold dim]terminal palette preview:[/bold dim]")
        sample_bg = bg if bg.startswith("#") else "#000000"
        sample_fg = fg if fg.startswith("#") else "#ffffff"
        # Two rows of the bg/fg combo with real text on top for readability check.
        console.print(
            f"  [{sample_fg} on {sample_bg}]"
            f"  the quick brown puppy jumps over a sleepy log   [/]"
        )
        console.print(
            f"  [{sample_fg} on {sample_bg}]"
            f"  bg={bg}  fg={fg}{' ' * max(0, 18 - len(bg) - len(fg))}  [/]"
        )

        # ANSI palette: 16 swatches in 2 rows of 8 so users see the rainbow.
        if ansi:
            console.print("[bold dim]ANSI palette (slots 0-15):[/bold dim]")
            for row_start in (0, 8):
                line = "  "
                for slot in range(row_start, min(row_start + 8, len(ansi))):
                    swatch_color = (
                        ansi[slot] if ansi[slot].startswith("#") else "#888888"
                    )
                    line += f"[on {swatch_color}]      [/]"
                console.print(line)

        console.print(
            f"[bold yellow]\u26a1[/bold yellow] [dim]Enter applies these to the whole terminal"
            f"{ansi_note}.[/dim]"
        )
    elif theme_name == "default":
        console.print(
            "[bold yellow]\u26a1[/bold yellow] [dim]Resets terminal bg/fg/ANSI palette too.[/dim]"
        )
    return buffer.getvalue()


def build_theme_menu(**menu_overrides):
    """Build the termflow Menu for the theme catalog.

    ``menu_overrides`` forward to the builder (``key_source``, ``output``,
    ``size``, ``alt_screen``) so tests can drive the menu headlessly.
    """
    # Stable seed for "Surprise Me" preview per highlight; bumps on each focus
    # so re-highlighting rolls fresh colors without flickering mid-highlight.
    surprise_seed = [random.randint(0, 1_000_000)]

    def _on_highlight(item: MenuItem) -> None:
        if item.value == "surprise":
            surprise_seed[0] = random.randint(0, 1_000_000)

    items = [
        MenuItem(
            f"{theme['icon']} {theme['label']}",
            value=name,
            description=theme["blurb"],
        )
        for name, theme in MENU
    ]
    builder = (
        MenuBuilder("Pick a Theme")
        .items(items)
        .page_size(THEMES_PER_PAGE)
        .list_width(LIST_WIDTH)
        .preview(lambda item: _render_preview(item.value, surprise_seed[0]))
        .on_highlight(_on_highlight)
        .footer_hint(
            "Up/Down navigate - PgUp/PgDn page - Enter apply - Esc cancel"
        )
    )
    for name, value in menu_overrides.items():
        getattr(builder, name)(value)
    return builder.build()


async def interactive_theme_picker() -> Optional[str]:
    """Show the full-screen theme picker.

    Returns:
        The selected theme key (e.g. ``"ocean"``) or ``None`` if cancelled.
    """
    from code_puppy.tools.command_runner import set_awaiting_user_input

    set_awaiting_user_input(True)
    try:
        # The menu blocks on raw stdin reads; keep the event loop breathing.
        result = await asyncio.to_thread(build_theme_menu().run)
    finally:
        set_awaiting_user_input(False)
    if result.cancelled or result.item is None:
        return None
    return result.item.value
