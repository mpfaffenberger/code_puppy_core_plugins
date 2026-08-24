"""Interactive terminal UI for configuring goal-mode LLM judges.

A split-panel list (left = judges, right = preview) on termflow.
Adding/editing chains focused widgets: a TextInput for the name, a
searchable menu for the model, and $EDITOR (TextInput fallback) for
the multiline prompt.

List view keys:
  N           add new judge (opens form)
  Enter / E   edit selected judge (opens form)
  T           toggle enabled
  D           delete selected
  Esc / Ctrl+C  close menu

Form view keys:
  Tab / Shift+Tab     cycle between Name ↔ Model ↔ Prompt
  ↑ / ↓               (when Model is focused) select model
  ←→ / PgUp PgDn      (when Model is focused) page through models
  Home / End          (when Model is focused) jump to first / last model
  Ctrl+S              save
  Esc / Ctrl+C        cancel
"""

from __future__ import annotations

import asyncio
import unicodedata
from typing import Optional

from code_puppy.command_line.model_picker_completion import load_model_names
from code_puppy.command_line.pagination import (
    ensure_visible_page,
    get_page_bounds,
    get_page_for_index,
    get_total_pages,
)
from code_puppy.messaging import emit_info, emit_success, emit_warning
from .judge_config import (
    DEFAULT_JUDGE_PROMPT,
    JudgeConfig,
    add_judge,
    delete_judge,
    load_judges,
    toggle_judge,
    update_judge,
    validate_name,
)
from code_puppy.tools.command_runner import set_awaiting_user_input

PAGE_SIZE = 10


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _sanitize(text: str) -> str:
    """Strip characters that mess with prompt_toolkit width calculations."""
    safe = (
        "Lu",
        "Ll",
        "Lt",
        "Lm",
        "Lo",
        "Nd",
        "Nl",
        "No",
        "Pc",
        "Pd",
        "Ps",
        "Pe",
        "Pi",
        "Pf",
        "Po",
        "Zs",
        "Sm",
        "Sc",
        "Sk",
    )
    cleaned = "".join(c for c in text if unicodedata.category(c) in safe)
    return " ".join(cleaned.split())


def _wrap(text: str, width: int) -> list[str]:
    """Crude word-wrap for the preview panel."""
    out: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            out.append("")
            continue
        words = line.split()
        current = ""
        for word in words:
            if not current:
                current = word
            elif len(current) + 1 + len(word) > width:
                out.append(current)
                current = word
            else:
                current += " " + word
        if current:
            out.append(current)
    return out


# ---------------------------------------------------------------------------
# Model list (inline paginated picker rendered as a tabbable form section)
# ---------------------------------------------------------------------------

MODEL_PAGE_SIZE = 8  # rows of models visible at once in the form section


def _load_available_models() -> list[str]:
    """Return the list of model names, or [] if loading fails."""
    try:
        return load_model_names() or []
    except Exception as exc:
        emit_warning(f"Failed to load models: {exc}")
        return []


def _render_model_list(
    models: list[str],
    selected_idx: int,
    page: int,
    *,
    focused: bool,
) -> list:
    """Render the inline paginated model list with a selection marker."""
    lines: list = []

    if not models:
        lines.append(("class:tui.warning", "  No models available."))
        lines.append(("", "\n"))
        lines.append(
            (
                "class:tui.muted",
                "  Configure models first — see /model in the main CLI.",
            )
        )
        return lines

    total_pages = get_total_pages(len(models), MODEL_PAGE_SIZE)
    start, end = get_page_bounds(page, len(models), MODEL_PAGE_SIZE)

    # Header: (Page x/y, focused indicator)
    if focused:
        lines.append(("class:tui.selected", "▼ "))
    else:
        lines.append(("class:tui.muted", "  "))
    lines.append(
        (
            "class:tui.muted",
            f"Page {page + 1}/{max(total_pages, 1)}   "
            f"(↑↓ to move, ←→ / PgUp PgDn to page)\n",
        )
    )

    for i in range(start, end):
        is_sel = i == selected_idx
        name = _sanitize(models[i])
        if is_sel and focused:
            lines.append(("class:tui.selected", "  ▶ "))
            lines.append(("class:tui.selected", name))
        elif is_sel:
            lines.append(("class:tui.warning", "  · "))
            lines.append(("class:tui.warning", name))
        else:
            lines.append(("", "    "))
            lines.append(("", name))
        lines.append(("", "\n"))

    return lines


# ---------------------------------------------------------------------------
# In-TUI form for add/edit
# ---------------------------------------------------------------------------


class _FormResult:
    """Mutable struct so closures in key bindings can mutate."""

    def __init__(self) -> None:
        self.saved: bool = False
        self.cancelled: bool = False
        self.name: str = ""
        self.model: str = ""
        self.prompt: str = ""


def _edit_prompt_in_editor(initial: str) -> Optional[str]:
    """Open $EDITOR on the judge prompt. None when unavailable/failed."""
    import os
    import subprocess
    import sys
    import tempfile

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    fd, path = tempfile.mkstemp(suffix=".md", prefix="judge_prompt_")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(initial)
        sys.__stdout__.write("\x1b[2J\x1b[H")
        sys.__stdout__.flush()
        if subprocess.call([*editor.split(), path]) != 0:
            return None
        with open(path) as handle:
            return handle.read().strip("\n")
    except Exception:
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _run_judge_form_sync(
    *,
    title: str,
    initial_name: str = "",
    initial_model: str = "",
    initial_prompt: str = DEFAULT_JUDGE_PROMPT,
) -> _FormResult:
    """Chained-widget form: name TextInput -> model menu -> prompt editor."""
    from termflow.tui import MenuBuilder, MenuItem, TextInputBuilder

    from code_puppy.command_line.tui_style import menu_style, themed

    result = _FormResult()
    style = menu_style()

    name_builder = (
        TextInputBuilder(f"{title} - Name")
        .prompt("Name: ")
        .initial(initial_name)
        .validator(lambda text: validate_name(text.strip()))
        .footer_hint("Enter continue - Esc cancel")
        .alt_screen(False)
    )
    if style is not None:
        name_builder.style(style)
    name_result = name_builder.build().run()
    if name_result.cancelled or not (name_result.value or "").strip():
        result.cancelled = True
        return result
    result.name = name_result.value.strip()

    models = _load_available_models()
    if models:
        initial_idx = models.index(initial_model) if initial_model in models else 0
        menu = themed(
            MenuBuilder(f"{title} - Model")
            .items([MenuItem(m, value=m) for m in models])
            .searchable()
            .initial_index(initial_idx)
            .alt_screen(False)
            .footer_hint("type filter - Enter select - Esc cancel")
        ).build()
        pick = menu.run()
        if pick.cancelled or pick.item is None:
            result.cancelled = True
            return result
        result.model = pick.item.value
    else:
        result.model = initial_model

    edited = _edit_prompt_in_editor(initial_prompt)
    if edited is not None:
        result.prompt = edited
    else:
        prompt_builder = (
            TextInputBuilder(f"{title} - Prompt (single line fallback)")
            .prompt("Prompt: ")
            .initial(initial_prompt.replace("\n", " ").strip())
            .footer_hint("Enter save - Esc cancel")
            .alt_screen(False)
        )
        if style is not None:
            prompt_builder.style(style)
        prompt_result = prompt_builder.build().run()
        if prompt_result.cancelled:
            result.cancelled = True
            return result
        result.prompt = prompt_result.value or initial_prompt

    result.saved = True
    return result


async def _run_judge_form(
    *,
    title: str,
    initial_name: str = "",
    initial_model: str = "",
    initial_prompt: str = DEFAULT_JUDGE_PROMPT,
) -> _FormResult:
    """Async wrapper: run the chained form off the event loop."""
    return await asyncio.to_thread(
        lambda: _run_judge_form_sync(
            title=title,
            initial_name=initial_name,
            initial_model=initial_model,
            initial_prompt=initial_prompt,
        )
    )


# ---------------------------------------------------------------------------
# Panel rendering for the list view
# ---------------------------------------------------------------------------


def _render_menu(
    judges: list[JudgeConfig],
    page: int,
    selected_idx: int,
) -> list:
    lines = []
    total_pages = get_total_pages(len(judges), PAGE_SIZE)
    start, end = get_page_bounds(page, len(judges), PAGE_SIZE)

    lines.append(("class:tui.header", "Goal Judges"))
    lines.append(("class:tui.muted", f" (Page {page + 1}/{max(total_pages, 1)})"))
    lines.append(("", "\n\n"))

    if not judges:
        lines.append(("class:tui.warning", "  No judges configured."))
        lines.append(("", "\n"))
        lines.append(("class:tui.muted", "  Press "))
        lines.append(("class:tui.help-key", "N"))
        lines.append(("class:tui.muted", " to add one."))
        lines.append(("", "\n\n"))
    else:
        for i in range(start, end):
            judge = judges[i]
            is_selected = i == selected_idx
            marker = "▶ " if is_selected else "  "
            row_style = "class:tui.selected" if is_selected else ""
            enabled_glyph = "[on] " if judge.enabled else "[off]"
            enabled_style = "class:tui.success" if judge.enabled else "class:tui.muted"

            lines.append((row_style or "class:tui.success", marker))
            lines.append((enabled_style, enabled_glyph + " "))
            lines.append((row_style, _sanitize(judge.name)))
            lines.append(("class:tui.muted", "  "))
            lines.append(("class:tui.warning", _sanitize(judge.model)))
            lines.append(("", "\n"))

    lines.append(("", "\n"))
    lines.append(("class:tui.help-key", "  ↑↓ "))
    lines.append(("", "Navigate\n"))
    lines.append(("class:tui.help-key", "  ←→ "))
    lines.append(("", "Page\n"))
    lines.append(("class:tui.help-key", "  N "))
    lines.append(("", "New judge\n"))
    lines.append(("class:tui.help-key", "  Enter "))
    lines.append(("", "Edit (or E)\n"))
    lines.append(("class:tui.help-key", "  T "))
    lines.append(("", "Toggle enabled\n"))
    lines.append(("class:tui.error", "  D "))
    lines.append(("", "Delete\n"))
    lines.append(("class:tui.help-key", "  Esc "))
    lines.append(("", "Close (or Ctrl+C)"))
    return lines


def _render_preview(judge: Optional[JudgeConfig]) -> list:
    lines = []
    lines.append(("class:tui.title", " JUDGE DETAILS"))
    lines.append(("", "\n\n"))

    if judge is None:
        lines.append(("class:tui.warning", "  No judge selected."))
        lines.append(("", "\n"))
        return lines

    lines.append(("class:tui.label", "Name: "))
    lines.append(("", _sanitize(judge.name)))
    lines.append(("", "\n\n"))

    lines.append(("class:tui.label", "Model: "))
    lines.append(("class:tui.warning", _sanitize(judge.model)))
    lines.append(("", "\n\n"))

    lines.append(("class:tui.label", "Enabled: "))
    if judge.enabled:
        lines.append(("class:tui.success", "yes"))
    else:
        lines.append(("class:tui.muted", "no"))
    lines.append(("", "\n\n"))

    lines.append(("class:tui.label", "Prompt:"))
    lines.append(("", "\n"))
    for wrapped in _wrap(judge.prompt or "", width=58):
        lines.append(("class:tui.muted", wrapped or " "))
        lines.append(("", "\n"))

    return lines


# ---------------------------------------------------------------------------
# Add / edit handlers (invoked between TUI sessions)
# ---------------------------------------------------------------------------


async def _add_judge_flow() -> Optional[str]:
    form = await _run_judge_form(title="New Judge")
    if not form.saved:
        emit_info("Cancelled.")
        return None
    try:
        add_judge(
            JudgeConfig(
                name=form.name,
                model=form.model,
                prompt=form.prompt,
                enabled=True,
            )
        )
    except ValueError as exc:
        emit_warning(str(exc))
        return None
    emit_success(f"Added judge {form.name!r} → {form.model}")
    return form.name


async def _edit_judge_flow(current: JudgeConfig) -> Optional[str]:
    form = await _run_judge_form(
        title=f"Edit Judge — {current.name}",
        initial_name=current.name,
        initial_model=current.model,
        initial_prompt=current.prompt,
    )
    if not form.saved:
        emit_info("Cancelled.")
        return current.name

    try:
        update_judge(
            current.name,
            new_name=form.name if form.name != current.name else None,
            model=form.model if form.model != current.model else None,
            prompt=form.prompt if form.prompt != current.prompt else None,
        )
    except ValueError as exc:
        emit_warning(str(exc))
        return current.name
    emit_success(f"Updated judge {form.name!r}")
    return form.name


# ---------------------------------------------------------------------------
# Main TUI loop
# ---------------------------------------------------------------------------


async def interactive_judges_menu() -> None:
    """Open the goal-judges TUI. Returns when the user closes the menu."""
    registry = load_judges()
    judges = list(registry.judges)

    selected_idx = [0]
    current_page = [0]

    def refresh(select_name: Optional[str] = None) -> None:
        nonlocal judges
        registry = load_judges()
        judges = list(registry.judges)
        if not judges:
            selected_idx[0] = 0
            current_page[0] = 0
            return
        if select_name:
            for i, j in enumerate(judges):
                if j.name == select_name:
                    selected_idx[0] = i
                    break
            else:
                selected_idx[0] = min(selected_idx[0], len(judges) - 1)
        else:
            selected_idx[0] = min(selected_idx[0], len(judges) - 1)
        current_page[0] = get_page_for_index(selected_idx[0], PAGE_SIZE)

    def current_judge() -> Optional[JudgeConfig]:
        if 0 <= selected_idx[0] < len(judges):
            return judges[selected_idx[0]]
        return None

    pending: list = [None, None]  # (action, target)

    def _render() -> list:
        from termflow.tui.terminal import terminal_size

        from code_puppy_core_plugins.termflow_tui import two_pane

        width, _ = terminal_size()
        usable = max(40, width - 1)
        return two_pane(
            _render_menu(judges, current_page[0], selected_idx[0]),
            _render_preview(current_judge()),
            width=usable,
            list_width=max(24, int(usable * 0.4)),
        )

    def _handle_key(key: str) -> bool:
        if key == "up":
            if selected_idx[0] > 0:
                selected_idx[0] -= 1
                current_page[0] = ensure_visible_page(
                    selected_idx[0], current_page[0], len(judges), PAGE_SIZE
                )
        elif key == "down":
            if selected_idx[0] < len(judges) - 1:
                selected_idx[0] += 1
                current_page[0] = ensure_visible_page(
                    selected_idx[0], current_page[0], len(judges), PAGE_SIZE
                )
        elif key == "left":
            if current_page[0] > 0:
                current_page[0] -= 1
                selected_idx[0] = current_page[0] * PAGE_SIZE
        elif key == "right":
            total = get_total_pages(len(judges), PAGE_SIZE)
            if current_page[0] < total - 1:
                current_page[0] += 1
                selected_idx[0] = current_page[0] * PAGE_SIZE
        elif key == "n":
            pending[0] = "add"
            return True
        elif key in ("enter", "e"):
            judge = current_judge()
            if judge:
                pending[0], pending[1] = "edit", judge.name
                return True
        elif key == "t":
            judge = current_judge()
            if judge:
                pending[0], pending[1] = "toggle", judge.name
                return True
        elif key == "d":
            judge = current_judge()
            if judge:
                pending[0], pending[1] = "delete", judge.name
                return True
        elif key in ("escape", "ctrl-c"):
            pending[0] = "close"
            return True
        return False

    from code_puppy.command_line.menu_session import menu_session

    from code_puppy_core_plugins.termflow_tui import FragmentTUI

    set_awaiting_user_input(True)
    try:
        with menu_session():
            while True:
                pending[0], pending[1] = None, None
                tui = FragmentTUI(_render, _handle_key, use_alt_screen=False)
                await asyncio.to_thread(tui.run)

                action, target = pending

                if action in (None, "close", "cancel"):
                    break

                if action == "add":
                    new_name = await _add_judge_flow()
                    refresh(select_name=new_name)
                    continue

                if not target:
                    continue

                if action == "edit":
                    judge = next((j for j in judges if j.name == target), None)
                    if judge:
                        new_name = await _edit_judge_flow(judge)
                        refresh(select_name=new_name or target)
                    continue

                if action == "toggle":
                    new_state = toggle_judge(target)
                    if new_state is None:
                        emit_warning(f"No judge named {target!r}.")
                    else:
                        emit_info(
                            f"{target!r} is now "
                            f"{'enabled' if new_state else 'disabled'}"
                        )
                    refresh(select_name=target)
                    continue

                if action == "delete":
                    if delete_judge(target):
                        emit_success(f"Deleted judge {target!r}")
                    else:
                        emit_warning(f"No judge named {target!r}.")
                    refresh()
                    continue
    finally:
        set_awaiting_user_input(False)

    emit_info("✓ Exited judges menu")
