"""Interactive terminal UI for browsing and installing remote agent skills.

Launched from `/skills install` (wiring may live elsewhere). Provides a
split-panel prompt_toolkit UI:
- Left: categories, then skills within a category
- Right: live details preview for the current selection

Installation happens after the TUI exits, with a confirmation prompt via
`safe_input()`, and uses `download_and_install_skill()` to fetch and extract
remote ZIPs.

This module is intentionally defensive: if the remote catalog isn't available,
it shows an empty menu and returns False.
"""

import logging
from pathlib import Path
from typing import List, Optional


from code_puppy.command_line.pagination import (
    ensure_visible_page,
    get_page_bounds,
    get_total_pages,
)
from code_puppy.command_line.utils import safe_input
from code_puppy.messaging import emit_error, emit_info, emit_success, emit_warning
from code_puppy_core_plugins.agent_skills.downloader import download_and_install_skill
from code_puppy_core_plugins.agent_skills.installer import InstallResult
from code_puppy_core_plugins.agent_skills.skill_catalog import (
    SkillCatalogEntry,
    catalog,
)
from code_puppy.tools.command_runner import set_awaiting_user_input

logger = logging.getLogger(__name__)

PAGE_SIZE = 12


def is_skill_installed(skill_id: str) -> bool:
    """Return True if the skill is already installed locally."""

    return (Path.home() / ".code_puppy" / "skills" / skill_id / "SKILL.md").is_file()


def _format_bytes(num_bytes: int) -> str:
    """Format bytes into a human-readable string."""

    try:
        size = float(max(0, int(num_bytes)))
    except Exception:
        return "0 B"

    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


def _wrap_text(text: str, width: int) -> List[str]:
    """Simple word-wrap for display in the details panel."""

    if not text:
        return []

    words = text.split()
    lines: list[str] = []
    current = ""

    for word in words:
        if not current:
            current = word
            continue

        if len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}"

    if current:
        lines.append(current)

    return lines


def _category_key(category: str) -> str:
    """Normalize a category string for icon lookup."""

    return "".join(ch for ch in (category or "").casefold() if ch.isalnum())


class SkillsInstallMenu:
    """Interactive TUI for browsing and installing remote skills."""

    def __init__(self):
        """Initialize the skills install menu with catalog data."""

        self.catalog = catalog
        self.categories: List[str] = []
        self.current_category: Optional[str] = None
        self.current_skills: List[SkillCatalogEntry] = []

        # State
        self.view_mode = "categories"  # categories | skills
        self.selected_category_idx = 0
        self.selected_skill_idx = 0
        self.current_page = 0
        self.result: Optional[str] = None
        self.pending_entry: Optional[SkillCatalogEntry] = None

        # UI controls

        self._initialize_catalog()

    def _initialize_catalog(self) -> None:
        """Load categories from the remote-backed catalog."""

        try:
            self.categories = self.catalog.list_categories() if self.catalog else []
        except Exception as e:
            emit_error(f"Skill catalog not available: {e}")
            self.categories = []

    def _get_category_icon(self, category: str) -> str:
        """Return an emoji icon for a skill category name."""

        icons = {
            "data": "📊",
            "finance": "💰",
            "legal": "⚖️",
            "office": "📄",
            "productmanagement": "📦",
            "sales": "💼",
            "biology": "🧬",
        }
        return icons.get(_category_key(category), "📁")

    def _get_current_category(self) -> Optional[str]:
        """Get the currently highlighted category name."""

        if 0 <= self.selected_category_idx < len(self.categories):
            return self.categories[self.selected_category_idx]
        return None

    def _get_current_skill(self) -> Optional[SkillCatalogEntry]:
        """Get the currently highlighted skill entry."""

        if self.view_mode == "skills" and self.current_skills:
            if 0 <= self.selected_skill_idx < len(self.current_skills):
                return self.current_skills[self.selected_skill_idx]
        return None

    def _render_navigation_hints(self, lines: List) -> None:
        """Render keyboard shortcut hints at the bottom."""

        lines.append(("", "\n"))
        lines.append(("class:tui.help-key", "  ↑/↓ "))
        lines.append(("", "Navigate  "))
        lines.append(("class:tui.help-key", "←/→ "))
        lines.append(("", "Page\n"))

        if self.view_mode == "categories":
            lines.append(("class:tui.help-key", "  Enter  "))
            lines.append(("", "Browse Skills\n"))
        else:
            lines.append(("class:tui.success", "  Enter  "))
            lines.append(("", "Install Skill\n"))
            lines.append(("class:tui.help-key", "  Esc/Back  "))
            lines.append(("", "Back\n"))

        lines.append(("class:tui.help-key", "  Ctrl+C "))
        lines.append(("", "Cancel"))

    def _render_category_list(self) -> List:
        """Render the left panel with category navigation."""

        lines = []

        lines.append(("class:tui.title", " 📂 CATEGORIES"))
        lines.append(("", "\n\n"))

        if not self.categories:
            lines.append(("class:tui.warning", "  No remote categories available."))
            lines.append(("", "\n"))
            lines.append(
                (
                    "class:tui.muted",
                    "  (Remote catalog unavailable or empty)\n",
                )
            )
            self._render_navigation_hints(lines)
            return lines

        total_pages = get_total_pages(len(self.categories), PAGE_SIZE)
        start_idx, end_idx = get_page_bounds(
            self.current_page, len(self.categories), PAGE_SIZE
        )

        for i in range(start_idx, end_idx):
            category = self.categories[i]
            is_selected = i == self.selected_category_idx
            icon = self._get_category_icon(category)
            count = 0
            try:
                count = (
                    len(self.catalog.get_by_category(category)) if self.catalog else 0
                )
            except Exception:
                count = 0

            prefix = " > " if is_selected else "   "
            label = f"{prefix}{icon} {category} ({count})"

            if is_selected:
                lines.append(("class:tui.selected", label))
            else:
                lines.append(("class:tui.muted", label))
            lines.append(("", "\n"))

        lines.append(("", "\n"))
        if total_pages > 1:
            lines.append(
                ("class:tui.muted", f" Page {self.current_page + 1}/{total_pages}")
            )
            lines.append(("", "\n"))

        self._render_navigation_hints(lines)
        return lines

    def _render_skill_list(self) -> List:
        """Render the middle panel with skills in the selected category."""

        lines = []

        if not self.current_category:
            lines.append(("class:tui.warning", "  No category selected."))
            lines.append(("", "\n\n"))
            self._render_navigation_hints(lines)
            return lines

        icon = self._get_category_icon(self.current_category)
        lines.append(("class:tui.title", f" {icon} {self.current_category.upper()}"))
        lines.append(("", "\n\n"))

        if not self.current_skills:
            lines.append(("class:tui.warning", "  No skills in this category."))
            lines.append(("", "\n\n"))
            self._render_navigation_hints(lines)
            return lines

        total_pages = get_total_pages(len(self.current_skills), PAGE_SIZE)
        start_idx, end_idx = get_page_bounds(
            self.current_page, len(self.current_skills), PAGE_SIZE
        )

        for i in range(start_idx, end_idx):
            entry = self.current_skills[i]
            is_selected = i == self.selected_skill_idx

            installed = is_skill_installed(entry.id)
            status_icon = "✓" if installed else "○"
            status_style = "class:tui.success" if installed else "class:tui.muted"

            prefix = " > " if is_selected else "   "
            label = f"{prefix}{status_icon} {entry.display_name}"

            if is_selected:
                lines.append(("class:tui.selected", label))
            else:
                lines.append((status_style, label))

            lines.append(("", "\n"))

        lines.append(("", "\n"))
        if total_pages > 1:
            lines.append(
                ("class:tui.muted", f" Page {self.current_page + 1}/{total_pages}")
            )
            lines.append(("", "\n"))

        self._render_navigation_hints(lines)
        return lines

    def _render_details(self) -> List:
        """Render the right panel with details for the selected skill."""

        lines = []

        lines.append(("class:tui.title", " 📋 DETAILS"))
        lines.append(("", "\n\n"))

        if self.view_mode == "categories":
            category = self._get_current_category()
            if not category:
                lines.append(("class:tui.warning", "  No category selected."))
                return lines

            icon = self._get_category_icon(category)
            lines.append(("bold", f"  {icon} {category}"))
            lines.append(("", "\n\n"))

            skills = []
            try:
                skills = self.catalog.get_by_category(category) if self.catalog else []
            except Exception:
                skills = []

            lines.append(("class:tui.muted", f"  {len(skills)} skills available"))
            lines.append(("", "\n\n"))

            # Show a preview of the first few skills
            if skills:
                lines.append(("bold", "  Preview:"))
                lines.append(("", "\n"))
                for entry in skills[:6]:
                    lines.append(("class:tui.muted", f"    • {entry.display_name}"))
                    lines.append(("", "\n"))

            return lines

        entry = self._get_current_skill()
        if not entry:
            lines.append(("class:tui.warning", "  No skill selected."))
            return lines

        installed = is_skill_installed(entry.id)
        installed_text = "Installed" if installed else "Not installed"
        installed_style = "class:tui.success" if installed else "class:tui.warning"

        lines.append(("bold", f"  {entry.display_name}"))
        lines.append(("", "\n"))
        lines.append((installed_style, f"  {installed_text}"))
        lines.append(("", "\n\n"))

        lines.append(("bold", "  ID:"))
        lines.append(("", "\n"))
        lines.append(("class:tui.muted", f"    {entry.id}"))
        lines.append(("", "\n\n"))

        lines.append(("bold", "  Description:"))
        lines.append(("", "\n"))
        desc = entry.description or "No description available"
        for line in _wrap_text(desc, 56):
            lines.append(("class:tui.muted", f"    {line}"))
            lines.append(("", "\n"))
        lines.append(("", "\n"))

        lines.append(("bold", "  Category:"))
        lines.append(("", "\n"))
        lines.append(("class:tui.muted", f"    {entry.category}"))
        lines.append(("", "\n\n"))

        lines.append(("bold", "  Tags:"))
        lines.append(("", "\n"))
        tags = entry.tags or []
        lines.append(
            ("class:tui.header", f"    {', '.join(tags) if tags else '(none)'}")
        )
        lines.append(("", "\n\n"))

        lines.append(("bold", "  Contents:"))
        lines.append(("", "\n"))
        lines.append(
            (
                "class:tui.muted",
                f"    scripts: {'yes' if entry.has_scripts else 'no'}",
            )
        )
        lines.append(("", "\n"))
        lines.append(
            (
                "class:tui.muted",
                f"    references: {'yes' if entry.has_references else 'no'}",
            )
        )
        lines.append(("", "\n"))
        lines.append(("class:tui.muted", f"    files: {entry.file_count}"))
        lines.append(("", "\n\n"))

        lines.append(("bold", "  Download:"))
        lines.append(("", "\n"))
        lines.append(
            (
                "class:tui.muted",
                f"    size: {_format_bytes(entry.zip_size_bytes)}",
            )
        )
        lines.append(("", "\n"))
        lines.append(("class:tui.muted", f"    url: {entry.download_url}"))
        lines.append(("", "\n"))

        return lines

    def update_display(self) -> None:
        """Rendering is pulled fresh each paint; nothing cached here."""

    def _enter_category(self) -> None:
        """Enter the currently highlighted category to browse skills."""

        category = self._get_current_category()
        if not category or not self.catalog:
            return

        self.current_category = category
        try:
            self.current_skills = self.catalog.get_by_category(category)
        except Exception:
            self.current_skills = []

        self.view_mode = "skills"
        self.selected_skill_idx = 0
        self.current_page = 0
        self.update_display()

    def _go_back_to_categories(self) -> None:
        """Navigate back from skill list to category list."""

        self.view_mode = "categories"
        self.current_category = None
        self.current_skills = []
        self.selected_skill_idx = 0
        self.current_page = 0
        self.update_display()

    def _select_current_skill(self) -> None:
        """Download and install the currently highlighted skill."""

        entry = self._get_current_skill()
        if entry:
            self.pending_entry = entry
            self.result = "pending_install"

    def handle_key(self, key: str) -> bool:
        """Dispatch one key. True exits the menu."""
        if key == "up":
            if self.view_mode == "categories":
                if self.selected_category_idx > 0:
                    self.selected_category_idx -= 1
                    self.current_page = ensure_visible_page(
                        self.selected_category_idx,
                        self.current_page,
                        len(self.categories),
                        PAGE_SIZE,
                    )
            elif self.selected_skill_idx > 0:
                self.selected_skill_idx -= 1
                self.current_page = ensure_visible_page(
                    self.selected_skill_idx,
                    self.current_page,
                    len(self.current_skills),
                    PAGE_SIZE,
                )
        elif key == "down":
            if self.view_mode == "categories":
                if self.selected_category_idx < len(self.categories) - 1:
                    self.selected_category_idx += 1
                    self.current_page = ensure_visible_page(
                        self.selected_category_idx,
                        self.current_page,
                        len(self.categories),
                        PAGE_SIZE,
                    )
            elif self.selected_skill_idx < len(self.current_skills) - 1:
                self.selected_skill_idx += 1
                self.current_page = ensure_visible_page(
                    self.selected_skill_idx,
                    self.current_page,
                    len(self.current_skills),
                    PAGE_SIZE,
                )
        elif key == "left":
            if self.current_page > 0:
                self.current_page -= 1
                if self.view_mode == "categories":
                    self.selected_category_idx = self.current_page * PAGE_SIZE
                else:
                    self.selected_skill_idx = self.current_page * PAGE_SIZE
        elif key == "right":
            total_items = (
                len(self.categories)
                if self.view_mode == "categories"
                else len(self.current_skills)
            )
            total_pages = get_total_pages(total_items, PAGE_SIZE)
            if self.current_page < total_pages - 1:
                self.current_page += 1
                if self.view_mode == "categories":
                    self.selected_category_idx = self.current_page * PAGE_SIZE
                else:
                    self.selected_skill_idx = self.current_page * PAGE_SIZE
        elif key == "enter":
            if self.view_mode == "categories":
                self._enter_category()
            else:
                self._select_current_skill()
                return True
        elif key in ("escape", "backspace"):
            if self.view_mode == "skills":
                self._go_back_to_categories()
            elif key == "escape":
                return True
        elif key == "ctrl-c":
            return True
        self.update_display()
        return False

    def _render(self) -> list:
        from termflow.tui.terminal import terminal_size

        from code_puppy_core_plugins.termflow_tui import two_pane

        width, _ = terminal_size()
        usable = max(40, width - 1)
        left = (
            self._render_category_list()
            if self.view_mode == "categories"
            else self._render_skill_list()
        )
        return two_pane(
            left,
            self._render_details(),
            width=usable,
            list_width=max(24, int(usable * 0.35)),
        )

    def run(self) -> bool:
        """Run the skills install menu. True if a skill was installed."""
        from code_puppy_core_plugins.termflow_tui import FragmentTUI

        set_awaiting_user_input(True)
        try:
            FragmentTUI(self._render, self.handle_key, use_alt_screen=True).run()
        finally:
            set_awaiting_user_input(False)

        # Handle install after the TUI exits
        if self.result == "pending_install" and self.pending_entry:
            return _prompt_and_install(self.pending_entry)

        emit_info("Exited skills install browser")
        return False


def _prompt_and_install(entry: SkillCatalogEntry) -> bool:
    """Prompt for confirmation and install the given skill."""

    installed = is_skill_installed(entry.id)
    size_str = _format_bytes(entry.zip_size_bytes)

    try:
        if installed:
            answer = safe_input(
                f"Skill '{entry.display_name}' is already installed. Reinstall ({size_str})? [y/N] "
            )
            if answer.strip().lower() not in {"y", "yes"}:
                emit_info("Installation cancelled")
                return False
            force = True
        else:
            answer = safe_input(
                f"Install skill '{entry.display_name}' ({size_str})? [y/N] "
            )
            if answer.strip().lower() not in {"y", "yes"}:
                emit_info("Installation cancelled")
                return False
            force = False

    except (KeyboardInterrupt, EOFError):
        emit_warning("Installation cancelled")
        return False

    emit_info(f"Downloading: {entry.display_name} ({size_str})")

    result: InstallResult
    try:
        result = download_and_install_skill(
            skill_name=entry.id,
            download_url=entry.download_url,
            force=force,
        )
    except Exception as e:
        logger.exception(f"Unexpected error during skill install: {e}")
        emit_error(f"Installation error: {e}")
        return False

    if result.success:
        emit_success(result.message)
        if result.installed_path:
            emit_info(f"Installed to: {result.installed_path}")
        return True

    emit_error(result.message)
    return False


def run_skills_install_menu() -> bool:
    """Run the bundled skills install menu.

    Returns:
        True if a skill was installed, False otherwise.
    """

    menu = SkillsInstallMenu()
    return menu.run()
