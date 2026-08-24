"""Full-screen queue manager for ``/queue``.

The application owns presentation state only. Queue mutations continue to flow
through :class:`PauseController`, keeping listeners and the bottom status bar in
sync with edits made here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_PREVIEW_CELLS = 58


def _preview(text: str, width: int = _PREVIEW_CELLS) -> str:
    """Return a compact single-line preview for a queued prompt."""
    flat = " ".join(text.split())
    if len(flat) <= width:
        return flat
    return flat[: max(0, width - 1)] + "…"


@dataclass
class QueueMenuState:
    """Small, testable state adapter around ``PauseController``."""

    controller: object
    selected: int = 0
    editing: bool = False
    adding: bool = False
    delete_armed: int | None = None
    notice: str = ""

    @property
    def items(self) -> list[str]:
        return self.controller.peek_pending_steer_queued()

    @property
    def selected_text(self) -> str:
        items = self.items
        if not items:
            return ""
        self.selected = min(max(self.selected, 0), len(items) - 1)
        return items[self.selected]

    def clamp_selection(self) -> None:
        self.selected = min(max(self.selected, 0), max(0, len(self.items) - 1))

    def move_selection(self, delta: int) -> None:
        if not self.items:
            self.selected = 0
            return
        self.selected = min(max(self.selected + delta, 0), len(self.items) - 1)
        self.delete_armed = None
        self.notice = ""

    def begin_add(self) -> None:
        self.editing = True
        self.adding = True
        self.delete_armed = None
        self.notice = "Adding prompt — Ctrl+S save · Esc cancel"

    def begin_edit(self) -> bool:
        if not self.items:
            self.notice = "Queue is empty — press A to add a prompt"
            return False
        self.editing = True
        self.adding = False
        self.delete_armed = None
        self.notice = "Editing prompt — Ctrl+S save · Esc cancel"
        return True

    def cancel_edit(self) -> None:
        self.editing = False
        self.adding = False
        self.notice = "Edit cancelled"

    def save(self, text: str) -> bool:
        normalized = text.strip()
        if not normalized:
            self.notice = "Prompt cannot be blank"
            return False

        items = self.items
        if self.adding:
            self.controller.request_steer(normalized, mode="queue")
            self.selected = len(items)
            self.notice = "Prompt added"
        elif self.selected < len(items):
            items[self.selected] = normalized
            self.controller.replace_pending_steer_queued(items)
            self.notice = "Prompt updated"
        else:
            self.notice = "That queue item no longer exists"
            return False

        self.editing = False
        self.adding = False
        self.delete_armed = None
        self.clamp_selection()
        return True

    def request_delete(self) -> bool:
        if not self.items:
            self.notice = "Queue is already empty"
            return False
        if self.delete_armed != self.selected:
            self.delete_armed = self.selected
            self.notice = "Press D again to delete this prompt"
            return False

        items = self.items
        del items[self.selected]
        self.controller.replace_pending_steer_queued(items)
        self.delete_armed = None
        self.clamp_selection()
        self.notice = "Prompt deleted"
        return True

    def reorder(self, delta: int) -> bool:
        items = self.items
        destination = self.selected + delta
        if not items or destination < 0 or destination >= len(items):
            return False
        items[self.selected], items[destination] = (
            items[destination],
            items[self.selected],
        )
        self.controller.replace_pending_steer_queued(items)
        self.selected = destination
        self.delete_armed = None
        self.notice = "Prompt moved"
        return True


class QueueMenuApp:
    """Persistent full-screen prompt queue manager, on termflow.

    While editing, a minimal inline buffer handles typing: printable
    keys append, Backspace deletes, Enter inserts a newline, Ctrl+S
    saves, Esc cancels.
    """

    def __init__(self, controller):
        self.state = QueueMenuState(controller)
        self._editor_text = self.state.selected_text
        self._exit = False

    # -- rendering ---------------------------------------------------------

    def _detail_title(self) -> str:
        if self.state.adding:
            return "New prompt"
        if self.state.editing:
            return f"Edit prompt {self.state.selected + 1}"
        return "Prompt preview"

    def _render_header(self):
        count = len(self.state.items)
        return [
            ("class:tui.title", "  PROMPT QUEUE\n"),
            ("class:tui.header", f"  {count} item{'s' if count != 1 else ''} waiting"),
        ]

    def _render_list(self):
        items = self.state.items
        if not items:
            return [
                (
                    "class:tui.muted",
                    "\n  Queue is empty.\n\n  Press A to add a prompt.",
                )
            ]
        fragments = []
        for index, text in enumerate(items):
            style = (
                "class:tui.selected"
                if index == self.state.selected
                else "class:tui.body"
            )
            marker = ">" if index == self.state.selected else " "
            fragments.extend(
                [
                    (style, f" {marker} "),
                    ("class:tui.muted", f"{index + 1:>2}  "),
                    (style, _preview(text)),
                    (style, "\n"),
                ]
            )
        return fragments

    def _render_detail(self):
        title = self._detail_title()
        body = self._editor_text if self.state.editing else self.state.selected_text
        fragments = [("class:tui.label", f" {title}\n\n")]
        if self.state.editing:
            fragments.append(("class:tui.label", "EDIT  "))
        fragments.append(("class:tui.body", body + ("_" if self.state.editing else "")))
        return fragments

    def _render_footer(self):
        if self.state.editing:
            return [
                ("class:tui.help", "  "),
                ("class:tui.help-key", "Ctrl+S"),
                ("class:tui.help", " save   "),
                ("class:tui.help-key", "Esc"),
                ("class:tui.help", " cancel   Enter inserts a newline"),
            ]
        return [
            ("class:tui.help", "  "),
            ("class:tui.help-key", "up/down or j/k"),
            ("class:tui.help", " select   "),
            ("class:tui.help-key", "Enter/E"),
            ("class:tui.help", " edit   "),
            ("class:tui.help-key", "A"),
            ("class:tui.help", " add   "),
            ("class:tui.help-key", "D D"),
            ("class:tui.help", " delete\n  "),
            ("class:tui.help-key", "[ ]"),
            ("class:tui.help", " reorder   "),
            ("class:tui.help-key", "Q/Esc"),
            ("class:tui.help", " done"),
        ]

    def _render(self) -> list:
        from termflow.tui.terminal import terminal_size

        from code_puppy_core_plugins.termflow_tui import (
            fragments_to_lines,
            two_pane,
        )

        width, _ = terminal_size()
        lines = fragments_to_lines(self._render_header())
        lines.append("")
        lines += two_pane(
            self._render_list(),
            self._render_detail(),
            width=max(40, width - 1),
            list_width=max(24, (width - 3) // 2),
        )
        lines.append("")
        lines += fragments_to_lines([("class:tui.warning", f"  {self.state.notice}")])
        lines += fragments_to_lines(self._render_footer())
        return lines

    # -- editing -----------------------------------------------------------

    def _sync_editor(self, text=None) -> None:
        self._editor_text = self.state.selected_text if text is None else text

    def _begin_add(self) -> None:
        self.state.begin_add()
        self._sync_editor("")

    def _begin_edit(self) -> None:
        if self.state.begin_edit():
            self._sync_editor()

    def _cancel_edit(self) -> None:
        self.state.cancel_edit()
        self._sync_editor()

    def _save_edit(self) -> None:
        if self.state.save(self._editor_text):
            self._sync_editor()

    def _move_selection(self, delta: int) -> None:
        self.state.move_selection(delta)
        self._sync_editor()

    # -- keys --------------------------------------------------------------

    def _handle_editing_key(self, key: str) -> bool:
        if key == "ctrl-s":
            self._save_edit()
        elif key in ("escape", "ctrl-c"):
            self._cancel_edit()
        elif key == "enter":
            self._editor_text += "\n"
        elif key == "backspace":
            self._editor_text = self._editor_text[:-1]
        elif key == "tab":
            self._editor_text += "    "
        elif key == " " or (len(key) == 1 and key.isprintable()):
            self._editor_text += key
        return False

    def handle_key(self, key: str) -> bool:
        """Dispatch one key. True exits the menu."""
        if self.state.editing:
            return self._handle_editing_key(key)
        if key in ("up", "k"):
            self._move_selection(-1)
        elif key in ("down", "j"):
            self._move_selection(1)
        elif key == "home":
            self.state.selected = 0
            self._sync_editor()
        elif key == "end" and self.state.items:
            self.state.selected = len(self.state.items) - 1
            self._sync_editor()
        elif key == "a":
            self._begin_add()
        elif key in ("e", "enter"):
            self._begin_edit()
        elif key == "d":
            if self.state.request_delete():
                self._sync_editor()
        elif key == "[":
            if self.state.reorder(-1):
                self._sync_editor()
        elif key == "]":
            if self.state.reorder(1):
                self._sync_editor()
        elif key in ("escape", "q", "ctrl-c"):
            return True
        return False

    async def run(self) -> None:
        import asyncio

        from code_puppy_core_plugins.termflow_tui import FragmentTUI

        tui = FragmentTUI(self._render, self.handle_key, use_alt_screen=True)
        await asyncio.to_thread(tui.run)


async def run_queue_menu() -> None:
    """Run the full-screen queue manager."""
    from code_puppy.messaging.pause_controller import get_pause_controller

    await QueueMenuApp(get_pause_controller()).run()


def open_queue_menu_blocking(timeout_s: float = 600.0) -> None:
    """Run the menu on a worker thread with an isolated event loop."""
    import asyncio
    import concurrent.futures

    try:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            executor.submit(lambda: asyncio.run(run_queue_menu())).result(
                timeout=timeout_s
            )
    except Exception:
        logger.debug("queue menu failed", exc_info=True)


__all__ = [
    "QueueMenuApp",
    "QueueMenuState",
    "open_queue_menu_blocking",
    "run_queue_menu",
]
