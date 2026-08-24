"""Interactive spinner picker TUI, on termflow.

Split-pane fragment renderer with a preview pane that actually
*animates*: the key-poll heartbeat doubles as the animation tick, and
the preview derives the current frame from wall-clock time, so every
spinner runs at its own configured interval.

Navigation follows the plugins-menu convention (same split-pane shape,
same muscle memory): ``j``/``k`` + arrows move the selection (clamped,
no wraparound), ``pageup``/``pagedown`` page through the list via the
shared pagination helpers, ``g``/``G``/``home``/``end`` jump to the
first/last entry, ``enter`` applies, and ``q``/``esc``/``ctrl-c`` bail.
``i`` writes the starter spinners.json without leaving; the animator
task also watches the file's mtime, so external edits reload the menu
live while the picker is open.
"""

from __future__ import annotations

import asyncio
import time
from typing import List, Optional, Tuple

from code_puppy.command_line.pagination import (
    ensure_visible_page,
    get_page_bounds,
    get_page_for_index,
    get_total_pages,
)

from code_puppy_core_plugins.termflow_tui import FragmentTUI, two_pane

from . import spinners as sp

FormattedText = list  # legacy alias: render fns return plain fragment lists

#: Preview invalidation cadence, pinned to MIN_INTERVAL so slowest spinners run
#: at their true speed.
_REFRESH_INTERVAL_S = sp.MIN_INTERVAL
PAGE_SIZE = 20  # one line per entry, same as the plugins menu
#: Speed-key grid step; matching the clamp floor keeps 0.02..1.0 on-grid.
_SPEED_STEP_S = 0.02


def _step_interval(current: float, delta: float) -> float:
    """One speed-key step from *current*: snap to the step grid, then clamp.

    Snapping matters at the edges: clamping alone strands you off-grid
    (0.05 -> floor 0.02 -> 0.07 -> 0.12 ...). Rounding to the nearest
    multiple of the step first means the floor is just a stop -- the
    next nudge up lands back on 0.05. Off-grid starting values (from a
    spinners.json tweak) get folded onto the grid the same way.
    """
    stepped = round((current + delta) / _SPEED_STEP_S) * _SPEED_STEP_S
    return sp.clamp_interval(stepped)


def _format_menu(
    entries: List[sp.Spinner], selected: int, page: int, active: str
) -> FormattedText:
    """Left-hand menu, styled to match the plugins menu's list pane.

    One line per entry: ``" > "`` marks the selection (bold), a green
    ``*`` glyph marks the active spinner (the plugins menu's ``+``/``x``
    slot), names only -- descriptions live in the preview pane, exactly
    like plugin descriptions live in the detail pane.
    """
    total_pages = get_total_pages(len(entries), PAGE_SIZE)
    start, end = get_page_bounds(page, len(entries), PAGE_SIZE)

    lines: list[tuple[str, str]] = [
        ("class:tui.header", " Spinners"),
        ("", "\n\n"),
    ]
    for i in range(start, end):
        spinner = entries[i]
        is_selected = i == selected
        icon = "*" if spinner.name == active else " "
        prefix = " > " if is_selected else "   "

        if is_selected:
            lines.append(("class:tui.selected", prefix))
            lines.append(("class:tui.selected", icon))
            lines.append(("class:tui.selected", f" {spinner.name}"))
        else:
            lines.append(("class:tui.body", prefix))
            lines.append(("class:tui.success", icon))
            lines.append(("class:tui.muted", f" {spinner.name}"))
        lines.append(("", "\n"))

    lines.append(("", "\n"))
    lines.append(("class:tui.muted", f" Page {page + 1}/{total_pages}"))
    lines.append(("", "\n"))

    _render_hints(lines)
    return FormattedText(lines)


#: Hint rows; ``_render_hints`` pads keys to keep action columns aligned.
_HINTS = [
    ("class:tui.help-key", "up/down or j/k", "Navigate"),
    ("class:tui.help-key", "PgUp/PgDn", "Page"),
    ("class:tui.help-key", "g / G", "First / Last"),
    ("class:tui.help-key", "-/+ or \u2190/\u2192", "Slower / Faster"),
    ("class:tui.help-key", "i", "Init spinners.json"),
    ("class:tui.success", "Enter", "Apply"),
    ("class:tui.error", "q / Esc", "Exit"),
]


def _render_hints(lines: list[tuple[str, str]]) -> None:
    """The plugins menu's hint block, with a speed row where their
    detail-scroll row sits. The key column is computed, not hand-padded.
    """
    key_col = max(len(keys) for _, keys, _ in _HINTS) + 1
    lines.append(("", "\n"))
    for i, (style, keys, label) in enumerate(_HINTS):
        lines.append((style, f"  {keys.ljust(key_col)}"))
        lines.append(
            ("class:tui.help", label if i == len(_HINTS) - 1 else f"{label}\n")
        )


def _format_preview(
    spinner: sp.Spinner,
    started_at: float,
    interval: Optional[float] = None,
    notice: str = "",
) -> FormattedText:
    """Right-hand pane, styled like the plugins menu's detail pane.

    *interval* is the speed the user dialed with the speed keys; None
    means the spinner's own speed (the usual case). *notice* is a
    one-line status message (e.g. the outcome of pressing ``i``).
    """
    effective = interval if interval is not None else spinner.interval
    elapsed = time.monotonic() - started_at
    frame = spinner.frames[int(elapsed / effective) % len(spinner.frames)]
    lines: list[tuple[str, str]] = [
        ("class:tui.title", " LIVE PREVIEW"),
        ("", "\n\n"),
        ("class:tui.label", f"  {spinner.name}"),
        ("", "\n\n"),
    ]
    if spinner.description:
        lines.append(("", f"  {spinner.description}"))
        lines.append(("", "\n\n"))
    lines.extend(
        [
            ("class:tui.label", f"  {frame}"),
            ("", "\n\n"),
            ("class:tui.label", "  Source: "),
            ("class:tui.body", spinner.source),
            ("", "\n"),
            ("class:tui.label", "  Frames: "),
            ("class:tui.body", str(len(spinner.frames))),
            ("", "\n"),
            ("class:tui.label", "  Interval: "),
            ("class:tui.body", f"{effective:.2f}s"),
            (
                "class:tui.warning",
                "  (custom -- Enter saves it)" if interval is not None else "",
            ),
            ("", "\n\n"),
            ("class:tui.label", "  Custom spinners:"),
            ("", "\n"),
            ("class:tui.muted", f"    {sp.USER_SPINNERS_FILE}"),
            ("", "\n"),
            ("class:tui.muted", "    (press i to write a starter file)"),
            ("", "\n"),
        ]
    )
    if notice:
        lines.append(("", "\n"))
        lines.append(("class:tui.warning", f"  {notice}"))
        lines.append(("", "\n"))
    return FormattedText(lines)


async def interactive_spinner_picker() -> Optional[Tuple[str, Optional[float]]]:
    """Show the full-screen spinner picker.

    Returns:
        ``(name, interval)`` -- *interval* is the dialed custom speed or
        None for the spinner's own speed -- or ``None`` if cancelled.
    """
    from code_puppy.tools.command_runner import set_awaiting_user_input

    entries = list(sp.get_catalogue().values())
    active = sp.get_active_spinner().name
    selected = [next((i for i, s in enumerate(entries) if s.name == active), 0)]
    page = [get_page_for_index(selected[0], PAGE_SIZE)]
    custom_interval: list[Optional[float]] = [None]
    notice = [""]
    result: list[Optional[Tuple[str, Optional[float]]]] = [None]
    started_at = time.monotonic()

    def _set_selection(new_idx: int) -> None:
        """Clamp selection and keep its page visible (plugins-menu contract).

        Moving resets the dialed speed -- each entry previews at its own
        speed until you nudge it.
        """
        selected[0] = max(0, min(new_idx, len(entries) - 1))
        page[0] = ensure_visible_page(selected[0], page[0], len(entries), PAGE_SIZE)
        custom_interval[0] = None

    def _nudge_speed(delta: float) -> None:
        current = (
            custom_interval[0]
            if custom_interval[0] is not None
            else entries[selected[0]].interval
        )
        custom_interval[0] = _step_interval(current, delta)

    def _refresh_entries() -> None:
        """Re-read the catalogue in place, keeping the selection pinned by
        name. Shared by the ``i`` key and the live file watcher."""
        current_name = entries[selected[0]].name
        entries[:] = list(sp.get_catalogue().values())
        _set_selection(
            next((i for i, s in enumerate(entries) if s.name == current_name), 0)
        )

    def _change_page(delta: int) -> None:
        """Move the page by *delta* (clamped) and jump selection to its head."""
        total_pages = get_total_pages(len(entries), PAGE_SIZE)
        new_page = max(0, min(page[0] + delta, total_pages - 1))
        if new_page == page[0]:
            return
        page[0] = new_page
        _set_selection(new_page * PAGE_SIZE)

    def _write_starter() -> None:
        try:
            created = sp.write_template()
            notice[0] = (
                "Starter file written -- edit it freely, changes apply live."
                if created
                else "spinners.json already exists -- edit it directly."
            )
        except OSError as exc:
            notice[0] = f"Could not write starter file: {exc}"
        _refresh_entries()

    def _render() -> list:
        from termflow.tui.terminal import terminal_size

        width, _ = terminal_size()
        return two_pane(
            _format_menu(entries, selected[0], page[0], active),
            _format_preview(
                entries[selected[0]], started_at, custom_interval[0], notice[0]
            ),
            width=max(40, width - 1),
            list_width=36,
        )

    def _handle_key(key: str) -> bool:
        if key in ("up", "ctrl-p", "j"):
            _set_selection(selected[0] - 1)
        elif key in ("down", "ctrl-n", "k"):
            _set_selection(selected[0] + 1)
        elif key == "page-up":
            _change_page(-1)
        elif key == "page-down":
            _change_page(+1)
        elif key in ("home", "g"):
            _set_selection(0)
        elif key in ("end", "G"):
            _set_selection(len(entries) - 1)
        elif key in ("left", "-"):
            _nudge_speed(+_SPEED_STEP_S)
        elif key in ("right", "+", "="):
            _nudge_speed(-_SPEED_STEP_S)
        elif key == "i":
            _write_starter()
        elif key == "enter":
            result[0] = (entries[selected[0]].name, custom_interval[0])
            return True
        elif key in ("q", "escape", "ctrl-c"):
            result[0] = None
            return True
        return False

    file_stamp = [sp.user_file_stamp()]

    def _tick() -> bool:
        """Animation heartbeat: reload on external edits, repaint always."""
        stamp = sp.user_file_stamp()
        if stamp != file_stamp[0]:
            file_stamp[0] = stamp
            _refresh_entries()
        return True

    from code_puppy.command_line.menu_session import menu_session

    set_awaiting_user_input(True)
    try:
        tui = FragmentTUI(
            _render,
            _handle_key,
            on_tick=_tick,
            poll_s=_REFRESH_INTERVAL_S,
            use_alt_screen=False,
        )
        with menu_session():
            await asyncio.to_thread(tui.run)
    finally:
        set_awaiting_user_input(False)

    return result[0]
