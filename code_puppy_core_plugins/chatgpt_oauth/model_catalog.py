"""Parse model metadata returned by the ChatGPT Codex ``/models`` endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT = 95

# Effective windows below this are catalog garbage (the smallest real Codex
# model serves ~131K). Treat them as absent so a hostile or buggy catalog
# can't force hyper-aggressive compaction with e.g. context_window=2.
MIN_EFFECTIVE_CONTEXT_WINDOW = 16_000


@dataclass(frozen=True)
class CodexModelInfo:
    """A discovered Codex model and its usable input-context budget."""

    name: str
    context_length: int | None = None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _effective_context_length(model: dict[str, Any]) -> int | None:
    """Match Codex's effective-window calculation for one catalog entry."""
    raw_context_length = _positive_int(model.get("context_window"))
    if raw_context_length is None:
        raw_context_length = _positive_int(model.get("max_context_window"))
    if raw_context_length is None:
        return None

    percentage = _positive_int(model.get("effective_context_window_percent"))
    if percentage is None or percentage > 100:
        percentage = DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT
    effective = raw_context_length * percentage // 100
    return effective if effective >= MIN_EFFECTIVE_CONTEXT_WINDOW else None


def parse_model_catalog(payload: Any) -> list[CodexModelInfo]:
    """Extract unique model names and effective context windows from a payload."""
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return []

    catalog: list[CodexModelInfo] = []
    seen: set[str] = set()
    for raw_model in payload["models"]:
        if not isinstance(raw_model, dict):
            continue
        name = raw_model.get("slug") or raw_model.get("id") or raw_model.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if name in seen:
            continue
        seen.add(name)
        catalog.append(
            CodexModelInfo(
                name=name,
                context_length=_effective_context_length(raw_model),
            )
        )
    return catalog


def fallback_catalog(model_names: Iterable[str]) -> list[CodexModelInfo]:
    """Build metadata-free entries for the conservative fallback model list."""
    return [CodexModelInfo(name=name) for name in model_names]
