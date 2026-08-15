"""Remote-backed skill catalog adapter.

This module provides a stable public interface for the rest of the codebase.
Historically, code_puppy used a local static catalog. We now source skills from
`remote_catalog.fetch_remote_catalog()` while keeping the same access patterns.

Public API:
    from code_puppy_core_plugins.agent_skills.skill_catalog import (
        SkillCatalog,
        SkillCatalogEntry,
        _format_display_name,
        catalog,
    )

If the remote catalog can't be fetched (and there's no cache), the catalog is
empty by default. Loading is deferred until a catalog-backed operation needs it.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from code_puppy_core_plugins.agent_skills.remote_catalog import fetch_remote_catalog

logger = logging.getLogger(__name__)
_RETRY_DELAY_SECONDS = 60


_ACRONYMS = {
    "ai",
    "api",
    "aws",
    "cli",
    "cpu",
    "csv",
    "db",
    "dns",
    "gpu",
    "html",
    "http",
    "https",
    "id",
    "json",
    "jwt",
    "k8s",
    "llm",
    "ml",
    "mvp",
    "oauth",
    "pdf",
    "psql",
    "qa",
    "rest",
    "rpc",
    "sdk",
    "sql",
    "ssh",
    "ssl",
    "tls",
    "tsv",
    "ui",
    "url",
    "utc",
    "uuid",
    "xml",
    "yaml",
    "yml",
}


def _format_display_name(skill_id: str) -> str:
    """Format a human-readable display name from a skill id.

    Examples:
        data-exploration -> Data Exploration
        pdf -> PDF

    This is intentionally simple and predictable.
    """

    cleaned = (skill_id or "").strip()
    if not cleaned:
        return ""

    parts = [p for p in cleaned.replace("_", "-").split("-") if p]
    formatted: list[str] = []

    for part in parts:
        lower = part.lower()
        if lower in _ACRONYMS:
            formatted.append(lower.upper())
        else:
            # Keep existing capitalization for words like "Nextflow" if provided,
            # otherwise just Title Case.
            formatted.append(part[:1].upper() + part[1:].lower())

    return " ".join(formatted)


@dataclass(frozen=True, slots=True)
class SkillCatalogEntry:
    """Catalog entry for a skill.

    Fields are designed to match the historical local catalog interface while
    including remote-only fields (download_url, zip_size_bytes).
    """

    id: str
    name: str
    display_name: str
    description: str
    category: str
    tags: List[str] = field(default_factory=list)
    source_path: Optional[Path] = None
    has_scripts: bool = False
    has_references: bool = False
    file_count: int = 0
    download_url: str = ""
    zip_size_bytes: int = 0


class SkillCatalog:
    """Remote skill catalog.

    This class is a simple in-memory index over remote catalog entries.
    """

    def __init__(self) -> None:
        """Initialize the skill catalog with empty indices."""

        self._entries: list[SkillCatalogEntry] = []
        self._by_id: dict[str, SkillCatalogEntry] = {}
        self._by_category: dict[str, list[SkillCatalogEntry]] = {}
        self._loaded = False
        self._next_retry_at = 0.0
        self._load_lock = threading.Lock()

    def _load(self) -> None:
        """Load the optional remote catalog, retrying after bounded failures."""

        if self._loaded or time.monotonic() < self._next_retry_at:
            return

        with self._load_lock:
            if self._loaded or time.monotonic() < self._next_retry_at:
                return

            try:
                remote = fetch_remote_catalog()
            except Exception as e:
                logger.debug(f"Failed to fetch remote catalog: {e}")
                self._next_retry_at = time.monotonic() + _RETRY_DELAY_SECONDS
                return

            if remote is None:
                logger.debug("Remote skill catalog unavailable; catalog will be empty.")
                self._next_retry_at = time.monotonic() + _RETRY_DELAY_SECONDS
                return

            entries: list[SkillCatalogEntry] = []

            for remote_entry in remote.entries:
                skill_id = remote_entry.name
                entry = SkillCatalogEntry(
                    id=skill_id,
                    name=remote_entry.name,
                    display_name=_format_display_name(remote_entry.name),
                    description=remote_entry.description,
                    category=remote_entry.group,
                    tags=[],
                    source_path=None,
                    has_scripts=remote_entry.has_scripts,
                    has_references=remote_entry.has_references,
                    file_count=remote_entry.file_count,
                    download_url=remote_entry.download_url,
                    zip_size_bytes=remote_entry.zip_size_bytes,
                )
                entries.append(entry)

            self._rebuild_indices(entries)
            self._loaded = True

            logger.info(
                f"Loaded remote skill catalog: {len(self._entries)} skills in "
                f"{len(self._by_category)} categories"
            )

    def _rebuild_indices(self, entries: list[SkillCatalogEntry]) -> None:
        """Rebuild internal lookup indices from the loaded entries."""

        self._entries = list(entries)
        self._by_id = {}
        self._by_category = {}

        for entry in self._entries:
            # Last one wins if duplicates somehow exist.
            self._by_id[entry.id] = entry

            cat_key = (entry.category or "").casefold()
            self._by_category.setdefault(cat_key, []).append(entry)

        # Keep category lists stable and predictable.
        for cat_entries in self._by_category.values():
            cat_entries.sort(key=lambda e: e.display_name.casefold())

        self._entries.sort(key=lambda e: e.display_name.casefold())

    def list_categories(self) -> List[str]:
        """List all categories."""

        self._load()
        categories = {e.category for e in self._entries if e.category}
        return sorted(categories, key=lambda c: c.casefold())

    def get_by_category(self, category: str) -> List[SkillCatalogEntry]:
        """Return all entries in a category (case-insensitive)."""

        self._load()
        if not category:
            return []
        return list(self._by_category.get(category.casefold(), []))

    def search(self, query: str) -> List[SkillCatalogEntry]:
        """Search by substring over id/name/display_name/description/tags/category."""

        self._load()
        q = (query or "").strip().casefold()
        if not q:
            return self.get_all()

        results: list[SkillCatalogEntry] = []
        for entry in self._entries:
            haystacks = [
                entry.id,
                entry.name,
                entry.display_name,
                entry.description,
                entry.category,
                " ".join(entry.tags),
            ]

            if any(q in (h or "").casefold() for h in haystacks):
                results.append(entry)

        return results

    def get_by_id(self, skill_id: str) -> Optional[SkillCatalogEntry]:
        """Get a skill entry by id (case-sensitive exact match)."""

        self._load()
        if not skill_id:
            return None
        return self._by_id.get(skill_id)

    def get_all(self) -> List[SkillCatalogEntry]:
        """Return all entries."""

        self._load()
        return list(self._entries)


# Singleton instance used by the rest of the codebase.
# NOTE: This must never crash import-time.
catalog = SkillCatalog()


__all__ = [
    "SkillCatalog",
    "SkillCatalogEntry",
    "_format_display_name",
    "catalog",
]
