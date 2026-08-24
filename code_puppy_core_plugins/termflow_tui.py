"""Shared termflow plumbing for plugin TUIs.

Every plugin menu historically rendered ``(style, text)`` fragment lists
(prompt_toolkit's FormattedText shape) with semantic ``class:tui.*``
style names. This module keeps those pure render functions working on
termflow: :func:`fragments_to_lines` maps the semantic vocabulary onto
the shared RenderStyle palette (colored piecewise per line so SGR never
bleeds across repaint boundaries), and :class:`FragmentTUI` is a
generic headless event loop -- injectable ``key_source`` / ``output`` /
``size`` / ``use_alt_screen``, resize repaints on the poll heartbeat,
and a key->handler dispatch table.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TextIO

Fragments = list  # list[tuple[str, str]]


def _sgr_for(style_class: str) -> tuple[str, str]:
    """Map a semantic ``class:tui.*`` (or raw) style name to SGR codes."""
    from termflow.ansi.codes import BOLD_ON, DIM_ON, RESET
    from termflow.ansi.color import fg_color
    from termflow.render.style import RenderStyle

    try:
        from code_puppy.command_line.tui_style import menu_style

        s = menu_style() or RenderStyle.default()
    except Exception:
        s = RenderStyle.default()

    bold_bright = f"{fg_color(s.bright)}{BOLD_ON}"
    dim_grey = f"{fg_color(s.grey)}{DIM_ON}"
    mapping = {
        "title": bold_bright,
        "header": bold_bright,
        "header_bold": bold_bright,
        "label": fg_color(s.head),
        "selected": f"{fg_color(s.head)}{BOLD_ON}",
        "selected_check": fg_color(s.head),
        "cursor": f"{fg_color(s.head)}{BOLD_ON}",
        "cursor_active": f"{fg_color(s.head)}{BOLD_ON}",
        "cursor_inactive": dim_grey,
        "success": fg_color(s.head),
        "warning": fg_color(s.error),
        "error": fg_color(s.error),
        "danger": fg_color(s.error),
        "muted": dim_grey,
        "text_dim": dim_grey,
        "help": dim_grey,
        "help_text": dim_grey,
        "help-key": f"{fg_color(s.head)}{BOLD_ON}",
        "help_key": f"{fg_color(s.head)}{BOLD_ON}",
        "accent": fg_color(s.symbol),
        "input": "",
        "input.focused": f"{fg_color(s.bright)}{BOLD_ON}",
        "body": "",
    }
    # Last class name wins ("class:tui.a class:tui.b" -> b), matching how
    # more-specific classes overrode base ones in the old styling.
    name = ""
    for token in style_class.split():
        if token.startswith("class:tui."):
            name = token[len("class:tui.") :]
        elif token.startswith("class:"):
            name = name or token[len("class:") :]
    prefix = mapping.get(name, "")
    from termflow.ansi.codes import RESET  # noqa: F811

    return (prefix, RESET if prefix else "")


def fragments_to_lines(fragments: Fragments) -> list[str]:
    """Flatten (style, text) fragments into per-line ANSI strings."""
    lines: list[str] = [""]
    for style_class, text in fragments:
        prefix, suffix = _sgr_for(style_class)
        for i, part in enumerate(str(text).split("\n")):
            if i:
                lines.append("")
            if part:
                lines[-1] += f"{prefix}{part}{suffix}" if prefix else part
    return lines


class FragmentTUI:
    """Generic full-frame fragment renderer with a key dispatch loop.

    ``render`` returns the current frame as a list of ANSI lines (use
    :func:`fragments_to_lines` on legacy fragment output). ``on_key``
    receives each key and returns True to exit the loop.
    """

    def __init__(
        self,
        render: Callable[[], list],
        on_key: Callable[[str], bool],
        *,
        on_tick: Callable[[], bool] | None = None,
        poll_s: float | None = None,
        key_source: Callable[[], str] | None = None,
        output: TextIO | None = None,
        size: Callable[[], tuple[int, int]] | None = None,
        use_alt_screen: bool = True,
    ) -> None:
        import sys

        from termflow.tui.keys import read_key
        from termflow.tui.menu import RESIZE_POLL_S
        from termflow.tui.terminal import terminal_size

        timeout = poll_s if poll_s is not None else RESIZE_POLL_S
        self._render = render
        self._on_key = on_key
        self._on_tick = on_tick
        self._read_key = key_source or (lambda: read_key(timeout=timeout))
        self._output = output if output is not None else sys.__stdout__
        self._size = size or terminal_size
        self._use_alt_screen = use_alt_screen

    def _paint(self) -> None:
        from termflow.tui.layout import truncate

        width, height = self._size()
        width = max(10, width - 1)
        lines = [str(line) for line in self._render()]
        frame = [truncate(line, width) for line in lines[: max(1, height)]]
        payload = "\x1b[H" + "".join(f"{line}\x1b[K\r\n" for line in frame) + "\x1b[J"
        self._output.write(payload)
        self._output.flush()

    def _loop(self) -> None:
        self._paint()
        last_size = self._size()
        while True:
            key = self._read_key()
            if key == "":
                size = self._size()
                if size != last_size:
                    last_size = size
                    self._paint()
                elif self._on_tick is not None and self._on_tick():
                    self._paint()
                continue
            if self._on_key(key):
                return
            self._paint()

    def run(self) -> None:
        """Run until the key handler returns True."""
        if self._use_alt_screen:
            from termflow.tui.terminal import alt_screen, raw_mode

            with raw_mode(), alt_screen(self._output):
                self._loop()
        else:
            self._loop()


def two_pane(
    left: Fragments, right: Fragments, width: int, list_width: int
) -> list[str]:
    """Compose two fragment panes side by side (collapses when narrow)."""
    from termflow.tui.layout import split_frame

    return split_frame(
        fragments_to_lines(left),
        fragments_to_lines(right),
        width=width,
        list_width=list_width,
        focus="left",
    )
