"""Interactive TUI for /prune — multi-select history pruner.

Renders conversation history as a flat checkable list:

    [ ]   001  asst   ✎   "I've updated the auth module..."
    [ ]   002  user        "now make it idempotent"
    [ ]   003  asst        "Let me think through..."

Selection rules:
    * Only whole messages are selectable. Their tool calls go with them.
    * Tool returns (ModelRequest with ToolReturnPart) are not directly
      selectable — they tag along with whatever message owns the
      matching ToolCallPart.
    * Locked rows (role=system, i.e. messages carrying a
      SystemPromptPart) cannot be toggled.

Returns a PruneSelection describing which messages to remove. The
caller owns the actual mutation.
"""

from __future__ import annotations

import shutil
from typing import List, Optional, Set, Tuple

from code_puppy_core_plugins.prune.prune_model import (
    SIDE_EFFECT_ICONS,
    ContextBudget,
    MessageEntry,
    PruneSelection,
    Row,
)
from code_puppy_core_plugins.prune.prune_render import render_detail, render_list


class PruneMenu:
    """Termflow split-panel TUI for /prune."""

    def __init__(
        self,
        entries: List[MessageEntry],
        *,
        budget: Optional[ContextBudget] = None,
    ) -> None:
        if not entries:
            raise ValueError("PruneMenu requires at least one entry")

        self.entries = entries
        self.budget = budget or ContextBudget()

        # Build newest-first rows; pure tool returns hide under their owning
        # assistant tool call instead of appearing as top-level entries.
        self.rows: List[Row] = [
            Row(message_idx=msg_idx)
            for msg_idx in range(len(entries) - 1, -1, -1)
            if not entries[msg_idx].is_pure_tool_return
        ]

        if not self.rows:
            raise ValueError("PruneMenu has no visible rows")

        self.cursor: int = 0
        self.selected_messages: Set[int] = set()  # message_idx values

        # Seed viewport dimensions for tests; run() replaces them with terminal
        # size.
        self.viewport_top: int = 0
        self._visible_rows: int = 20

        self._result: Optional[PruneSelection] = None

    # ── selection logic ───────────────────────────────────────────────────

    def _toggle_current(self) -> None:
        row = self.rows[self.cursor]
        # Locked rows carry a SystemPromptPart and are non-toggleable.
        if self.entries[row.message_idx].is_locked:
            return
        if row.message_idx in self.selected_messages:
            self.selected_messages.discard(row.message_idx)
        else:
            self.selected_messages.add(row.message_idx)

    def _select_all(self) -> None:
        for msg_idx, entry in enumerate(self.entries):
            if entry.is_pure_tool_return or entry.is_locked:
                continue
            self.selected_messages.add(msg_idx)

    def _clear_all(self) -> None:
        self.selected_messages.clear()

    def _row_is_checked(self, row: Row) -> bool:
        return row.message_idx in self.selected_messages

    # ── viewport / pagination ──────────────────────────────────────────────────

    def _page_size(self) -> int:
        """Number of row lines that fit in the list pane right now."""
        # Always keep the page size at least 1 so we never divide by zero.
        return max(1, self._visible_rows)

    def _scroll_into_view(self) -> None:
        """Adjust viewport_top so cursor stays visible. Idempotent."""
        page = self._page_size()
        if self.cursor < self.viewport_top:
            self.viewport_top = self.cursor
        elif self.cursor >= self.viewport_top + page:
            self.viewport_top = self.cursor - page + 1
        # Clamp so we don't show empty space past the end
        max_top = max(0, len(self.rows) - page)
        if self.viewport_top > max_top:
            self.viewport_top = max_top
        if self.viewport_top < 0:
            self.viewport_top = 0

    # ── rendering (delegated to prune_render) ───────────────────────────────

    def _selection_has_side_effects(self) -> bool:
        for msg_idx in self.selected_messages:
            for tc in self.entries[msg_idx].tool_calls:
                if tc.icon in SIDE_EFFECT_ICONS:
                    return True
        return False

    def _update_display(self) -> None:
        self._scroll_into_view()

    # ── main entry ────────────────────────────────────────────────────────

    def _build_selection(self) -> PruneSelection:
        sel = PruneSelection()
        for msg_idx in self.selected_messages:
            sel.history_indices_to_drop.add(self.entries[msg_idx].history_index)
        return sel

    def handle_key(self, key: str) -> bool:
        """Dispatch one key. True exits the menu."""
        if key in ("up", "ctrl-p", "k"):
            if self.cursor > 0:
                self.cursor -= 1
        elif key in ("down", "ctrl-n", "j"):
            if self.cursor < len(self.rows) - 1:
                self.cursor += 1
        elif key == "page-up":
            self.cursor = max(0, self.cursor - self._page_size())
        elif key == "page-down":
            self.cursor = min(len(self.rows) - 1, self.cursor + self._page_size())
        elif key == "home":
            self.cursor = 0
        elif key == "end":
            self.cursor = len(self.rows) - 1
        elif key == " ":
            self._toggle_current()
        elif key == "a":
            self._select_all()
        elif key == "c":
            self._clear_all()
        elif key == "enter":
            self._result = self._build_selection()
            return True
        elif key in ("q", "escape", "ctrl-c"):
            self._result = None
            return True
        self._update_display()
        return False

    def _measure_terminal(self) -> Tuple[int, int]:
        """Return (cols, rows) of the current terminal, with sane fallbacks."""
        try:
            size = shutil.get_terminal_size(fallback=(120, 40))
            return max(60, size.columns), max(15, size.lines)
        except Exception:
            return 120, 40

    def _render(self) -> list:
        from code_puppy_core_plugins.termflow_tui import two_pane

        cols, rows = self._measure_terminal()
        self._visible_rows = max(5, rows - 12)
        self._update_display()
        usable = max(40, cols - 3)
        return two_pane(
            render_list(self),
            render_detail(self),
            width=usable,
            list_width=usable // 2,
        )

    def run(self) -> Optional[PruneSelection]:
        from code_puppy_core_plugins.termflow_tui import FragmentTUI

        try:
            from code_puppy.tools.command_runner import set_awaiting_user_input

            set_awaiting_user_input(True)
        except Exception:
            pass
        try:
            FragmentTUI(self._render, self.handle_key, use_alt_screen=True).run()
        finally:
            try:
                from code_puppy.tools.command_runner import set_awaiting_user_input

                set_awaiting_user_input(False)
            except Exception:
                pass
        return self._result


__all__ = ["PruneMenu"]
