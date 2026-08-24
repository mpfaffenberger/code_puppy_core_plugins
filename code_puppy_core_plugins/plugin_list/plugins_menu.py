"""Interactive TUI for managing plugins.

Launch with ``/plugins`` to browse and toggle plugins on/off.
Built on termflow, following the same pattern as the skills menu.

This module is the *controller*: terminal sizing, key bindings, app lifecycle,
and plugin state mutation. All rendering (fragment construction, padding,
emoji stripping) lives in :mod:`plugins_menu_render` so each module has one
reason to change.
"""

from __future__ import annotations

import shutil
from typing import List, Optional, Tuple

from code_puppy.command_line.pagination import (
    ensure_visible_page,
    get_total_pages,
)
from code_puppy_core_plugins.plugin_list.plugin_text_utils import (
    Fragments,
    count_lines,
    drop_leading_lines,
)
from code_puppy_core_plugins.plugin_list.plugins_menu_render import (
    fill_pane,
    render_detail,
    render_list,
)
from code_puppy.tools.command_runner import set_awaiting_user_input


class _TrustInput:
    """Minimal stand-in for the old TextArea: just a text buffer."""

    def __init__(self) -> None:
        self.text = ""


PAGE_SIZE = 20


class _PluginEntry:
    """Lightweight struct for a plugin row.

    ``status`` is "loaded" for imported plugins; project plugins held back
    by the trust gate carry their gate status instead ("untrusted",
    "changed", "disabled", "error") so the TUI can show them without
    pretending they're active.
    """

    __slots__ = ("name", "tier", "status")

    def __init__(self, name: str, tier: str, status: str = "loaded") -> None:
        self.name = name
        self.tier = tier
        self.status = status


class PluginsMenu:
    """Interactive TUI for enabling/disabling plugins.

    The view (``plugins_menu_render``) reads the following attributes on this
    object — keep them stable to avoid breaking the render contract:

    * ``plugins``, ``disabled``, ``selected_idx``, ``current_page``, ``page_size``
    * ``project_dir`` (posix string or None)
    * ``trust_target``, ``trust_error``, ``trust_feedback`` (popup state)
    * ``_detail_cols``, ``_pane_rows``
    * ``_changed``
    * ``_current()``

    Entries carry a ``status`` ("loaded" or a trust-gate status) — the
    renderer branches on it, so keep it stable too.
    """

    def __init__(self, focus_plugin: Optional[str] = None) -> None:
        """*focus_plugin* preselects that plugin; if it's trust-gated, the
        risk-acceptance popup opens immediately (used by ``/plugins enable``
        to bring the user straight to the ceremony)."""
        self.plugins: List[_PluginEntry] = []
        self.disabled: set[str] = set()
        self.project_dir: Optional[str] = None
        self.lock_builtin: bool = False
        self.hidden_builtin_count: int = 0

        # The trust modal disables list bindings while open, keeping accept-word
        # typing from triggering shortcuts; focus remains on its input.
        self.trust_target: Optional[_PluginEntry] = None
        self.trust_error: str = ""
        self.trust_feedback: str = ""
        self.trust_input = _TrustInput()

        self.selected_idx = 0
        self.current_page = 0
        # Mirrors PAGE_SIZE so the renderer's pagination math and the
        # keybindings (which use the module constant) can't drift apart.
        self.page_size = PAGE_SIZE
        self.result: Optional[str] = None
        self._changed = False

        self.detail_scroll = 0

        # Track pane height so cell-diff redraws overwrite stale glyphs below
        # short content.
        self._menu_cols = 30
        self._detail_cols = 60
        self._pane_rows = 20
        self._last_size: Tuple[int, int] = (0, 0)

        self._refresh_data()

        if focus_plugin:
            for i, entry in enumerate(self.plugins):
                if entry.name == focus_plugin:
                    self.selected_idx = i
                    self.current_page = ensure_visible_page(
                        i, self.current_page, len(self.plugins), PAGE_SIZE
                    )
                    if entry.status in ("untrusted", "changed"):
                        self._open_trust_modal(entry)
                    break

    # -- data helpers ------------------------------------------------------

    def _refresh_data(self) -> None:
        from code_puppy.plugins import (
            get_loaded_plugins,
            get_project_plugin_status,
            get_project_plugins_directory,
        )
        from code_puppy.plugins.config import get_disabled_plugins
        from code_puppy.plugins.config import (
            get_lock_builtin_plugins,
        )

        loaded = get_loaded_plugins()
        self.disabled = get_disabled_plugins()
        self.lock_builtin = get_lock_builtin_plugins()

        project_dir = get_project_plugins_directory()
        self.project_dir = project_dir.as_posix() if project_dir else None

        entries: List[_PluginEntry] = []
        self.hidden_builtin_count = 0
        for tier in ("builtin", "user", "project"):
            for name in sorted(loaded.get(tier, [])):
                # When locked, builtins are managed/protected — hide them so
                # they can't be toggled (the config layer refuses anyway).
                if self.lock_builtin and tier == "builtin":
                    self.hidden_builtin_count += 1
                    continue
                entries.append(_PluginEntry(name, tier))

        # Project plugins held back by the trust gate — shown so Enter can
        # open the ceremony popup on them.
        statuses = get_project_plugin_status()
        shown = {e.name for e in entries if e.tier == "project"}
        for name in sorted(statuses):
            if statuses[name] != "loaded" and name not in shown:
                entries.append(_PluginEntry(name, "project", statuses[name]))

        self.plugins = entries

        # Keep selection in range if the list shrank.
        if self.selected_idx >= len(self.plugins):
            self.selected_idx = max(0, len(self.plugins) - 1)

    def _current(self) -> Optional[_PluginEntry]:
        if 0 <= self.selected_idx < len(self.plugins):
            return self.plugins[self.selected_idx]
        return None

    def _toggle_current(self) -> None:
        entry = self._current()
        if not entry:
            return
        self.trust_feedback = ""

        if entry.status in ("untrusted", "changed"):
            # Ceremony required — open the risk-acceptance popup.
            self._open_trust_modal(entry)
            return
        if entry.status in ("disabled", "error"):
            # Already trusted; just (re)activate — no ceremony needed.
            from code_puppy_core_plugins.plugin_list.project_trust_flow import (
                activate_project_plugin,
            )

            _ok, message = activate_project_plugin(entry.name)
            self.trust_feedback = message
            self.detail_scroll = 0
            self._refresh_data()
            self.update_display()
            return

        from code_puppy.plugins.config import set_plugin_disabled

        is_disabled = entry.name in self.disabled
        changed = set_plugin_disabled(entry.name, not is_disabled)
        if changed:
            self._changed = True
            self.detail_scroll = 0
        self._refresh_data()
        self.update_display()

    # -- trust popup ---------------------------------------------------------

    def _open_trust_modal(self, entry: _PluginEntry) -> None:
        self.trust_target = entry
        self.trust_error = ""
        self.trust_input.text = ""
        self.update_display()

    def _close_trust_modal(self) -> None:
        self.trust_target = None
        self.trust_error = ""
        self.trust_input.text = ""
        self.update_display()

    def _accept_trust(self, buff) -> bool:
        """Accept-handler for the popup input. Returns False to clear the box."""
        entry = self.trust_target
        if entry is None:
            return False
        from code_puppy_core_plugins.plugin_list.project_trust_flow import (
            ACCEPT_WORD,
            grant_trust_and_load,
        )

        if buff.text.strip().lower() != ACCEPT_WORD:
            self.trust_error = (
                f"That isn't '{ACCEPT_WORD}' — type it exactly, or press Esc to cancel."
            )
            self.update_display()
            return False

        _ok, message = grant_trust_and_load(entry.name)
        self.trust_feedback = message
        self._refresh_data()
        self._close_trust_modal()
        return False

    # -- render compatibility ---------------------------------------------

    def _render_list(self) -> Fragments:
        """Return rendered list fragments for existing tests/callers."""
        return render_list(self)

    def _render_detail(self) -> Fragments:
        """Return rendered detail fragments for existing tests/callers."""
        return render_detail(self)

    # -- display update ----------------------------------------------------

    def update_display(self) -> None:
        # Rendering is pulled fresh in _render(); nothing cached here.
        pass

    def _max_detail_scroll(self) -> int:
        """Topmost line we may scroll to, keeping a screenful visible."""
        total = count_lines(render_detail(self))
        visible = max(1, self._pane_rows)
        return max(0, total - visible)

    def _scroll_detail(self, delta: int) -> None:
        new = max(0, min(self.detail_scroll + delta, self._max_detail_scroll()))
        if new != self.detail_scroll:
            self.detail_scroll = new
            self.update_display()

    # -- application -------------------------------------------------------

    def _measure_terminal(self) -> Tuple[int, int]:
        """Return (cols, rows) of the current terminal, with sane fallbacks."""
        try:
            size = shutil.get_terminal_size(fallback=(120, 40))
            return max(60, size.columns), max(15, size.lines)
        except Exception:
            return 120, 40

    def _recompute_dimensions(self) -> bool:
        """Re-measure the terminal and recompute pane widths.

        Returns True when the size actually changed. The width-callable
        closures in ``run`` read ``self._menu_cols`` / ``self._detail_cols``
        on every render, so updating these here automatically reflows the
        layout on terminal resize.
        """
        cols, rows = self._measure_terminal()
        if self._last_size == (cols, rows):
            return False
        self._last_size = (cols, rows)
        # Two side-by-side Frames cost 4 columns of border (1 per side, per
        # frame). Anything more leaves dead space on the right edge.
        usable_cols = max(40, cols - 4)
        # 35% / 65% split, with a minimum so the menu pane is always usable.
        self._menu_cols = max(20, min(40, int(usable_cols * 0.35)))
        self._detail_cols = max(20, usable_cols - self._menu_cols)
        # Reserve 2 rows for the Frame's top + bottom borders.
        self._pane_rows = max(5, rows - 2)
        return True

    def _set_selection(self, new_idx: int) -> None:
        """Move selection to *new_idx* (clamped), resetting detail scroll.

        Single chokepoint for every selection mutation -- ``_move_selection``,
        the jump-to-first/last actions, and the page jumps all funnel through
        here so the "reset detail scroll + keep selection's page visible"
        contract can't drift between callers.
        """
        if not self.plugins:
            return
        new_idx = max(0, min(new_idx, len(self.plugins) - 1))
        if new_idx == self.selected_idx:
            return
        self.selected_idx = new_idx
        self.detail_scroll = 0
        self.current_page = ensure_visible_page(
            self.selected_idx,
            self.current_page,
            len(self.plugins),
            PAGE_SIZE,
        )

    def _move_selection(self, delta: int) -> None:
        """Shift the selection by *delta*, clamped, and keep the page in view."""
        self._set_selection(self.selected_idx + delta)

    def _change_page(self, delta: int) -> None:
        """Move the page by *delta* (clamped) and jump selection to its head."""
        total_pages = get_total_pages(len(self.plugins), PAGE_SIZE)
        new_page = max(0, min(self.current_page + delta, total_pages - 1))
        if new_page == self.current_page:
            return
        self.current_page = new_page
        self._set_selection(self.current_page * PAGE_SIZE)

    def _render(self) -> list:
        from code_puppy_core_plugins.termflow_tui import fragments_to_lines

        self._recompute_dimensions()
        lines = []
        left = fragments_to_lines(
            fill_pane(render_list(self), self._menu_cols, self._pane_rows)
        )
        sliced = drop_leading_lines(render_detail(self), self.detail_scroll)
        right = fragments_to_lines(
            fill_pane(sliced, self._detail_cols, self._pane_rows)
        )
        from termflow.tui.layout import two_columns

        cols, _rows = self._measure_terminal()
        lines += two_columns(left, right, self._menu_cols + 1, max(40, cols - 1))
        if self.trust_target is not None:
            lines.append("")
            lines += fragments_to_lines(self._render_trust_prompt())
        return lines

    def _render_trust_prompt(self) -> Fragments:
        from code_puppy_core_plugins.plugin_list.project_trust_flow import ACCEPT_WORD

        fragments: Fragments = [
            (
                "class:tui.warning",
                f" Trust '{self.trust_target.name}'? Type '{ACCEPT_WORD}' and press"
                " Enter (Esc cancels): ",
            ),
            ("class:tui.selected", self.trust_input.text + "_"),
        ]
        if self.trust_error:
            fragments.append(("class:tui.error", f"\n {self.trust_error}"))
        return fragments

    def _handle_modal_key(self, key: str) -> bool:
        if key in ("escape", "ctrl-c"):
            self._close_trust_modal()
        elif key == "enter":
            self._accept_trust(self.trust_input)
        elif key == "backspace":
            self.trust_input.text = self.trust_input.text[:-1]
        elif len(key) == 1 and key.isprintable():
            self.trust_input.text += key
        return False

    def handle_key(self, key: str) -> bool:
        """Dispatch one key. True exits the menu."""
        if self.trust_target is not None:
            return self._handle_modal_key(key)
        if key in ("up", "ctrl-p", "j"):
            self._move_selection(-1)
        elif key in ("down", "ctrl-n", "k"):
            self._move_selection(+1)
        elif key == "page-up":
            self._change_page(-1)
        elif key == "page-down":
            self._change_page(+1)
        elif key in ("home", "g"):
            self._set_selection(0)
        elif key in ("end", "G"):
            self._set_selection(len(self.plugins) - 1)
        elif key in ("h", "left"):
            self._scroll_detail(-1)
        elif key in ("l", "right"):
            self._scroll_detail(+1)
        elif key == "enter":
            self._toggle_current()
            self.result = "changed"
        elif key in ("q", "escape", "ctrl-c"):
            self.result = "quit"
            return True
        return False

    def run(self) -> Optional[str]:
        from code_puppy_core_plugins.termflow_tui import FragmentTUI

        set_awaiting_user_input(True)
        try:
            FragmentTUI(self._render, self.handle_key, use_alt_screen=True).run()
        finally:
            set_awaiting_user_input(False)
        return self.result


def run_plugins_menu(focus_plugin: Optional[str] = None) -> Optional[str]:
    """Entry point: create and run the plugins TUI, return the result.

    *focus_plugin* preselects a plugin and, when it's trust-gated, opens
    the risk-acceptance popup straight away.
    """
    from code_puppy.messaging import emit_warning

    menu = PluginsMenu(focus_plugin=focus_plugin)
    result = menu.run()

    if menu._changed:
        emit_warning("Restart Code Puppy for plugin changes to take effect.")

    return result


# Re-export for callers that don't want to know about the render split.
__all__ = ["PluginsMenu", "Fragments", "run_plugins_menu"]
