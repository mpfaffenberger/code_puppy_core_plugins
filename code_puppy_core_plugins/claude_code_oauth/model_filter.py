"""Claude model generation filtering."""

import logging
import re
from typing import Dict, List, Tuple, Union


logger = logging.getLogger(__name__)

# claude-{family}-{major}[-|.{minor}][-{yyyymmdd}], e.g. claude-opus-5,
# claude-opus-5-5, claude-sonnet-4.5-20250929, claude-opus-4-20250514.
# The lookaheads stop a date from being misread as a minor version
# (claude-opus-4-20250514 is 4.0, not 4.20). Undated names rank date 0.
_MODEL_RE = re.compile(
    r"claude-(haiku|sonnet|opus|fable)-(\d+)"
    r"(?:[-.](\d{1,2})(?!\d))?"
    r"(?:-(\d{8}))?(?![\d.])"
)


def _parse_model(model_name: str) -> Tuple[str, int, int, int] | None:
    """Parse a model name into ``(family, major, minor, date)``."""
    match = _MODEL_RE.match(model_name)
    if not match:
        return None
    family, major, minor, date = match.groups()
    return family, int(major), int(minor or 0), int(date or 0)


def filter_latest_claude_models(
    models: List[str], max_per_family: Union[int, Dict[str, int]] = 2
) -> List[str]:
    """Filter models to keep the top N latest haiku, sonnet, and opus.

    Parses model names in the format claude-{family}-{major}-{minor}-{date}
    and returns the top ``max_per_family`` versions of each family
    (haiku, sonnet, opus), sorted newest-first.

    Args:
        models: List of model name strings to filter.
        max_per_family: Either a single int applied to all families, or a dict
            mapping family name to its limit (e.g. ``{"opus": 3}``). Families
            not present in the dict fall back to ``"default"`` key, or ``2``.
    """
    # Dedupe while preserving order: base and "-long" config entries share the
    # same underlying model name, and a duplicate must not consume a slot of
    # the per-family limit (e.g. [opus-5, opus-5, opus-4-8] silently dropping
    # opus-4-7 at limit 3).
    models = list(dict.fromkeys(models))

    # Collect all parsed models per family
    # family -> list of (model_name, major, minor, date)
    family_models: Dict[str, List[Tuple[str, int, int, int]]] = {}

    for model_name in models:
        parsed = _parse_model(model_name)
        if parsed is None:
            continue
        family, major, minor, date = parsed
        family_models.setdefault(family, []).append((model_name, major, minor, date))

    # Sort each family descending and keep the top N
    filtered: List[str] = []
    for family, family_entries in family_models.items():
        if isinstance(max_per_family, dict):
            limit = max_per_family.get(family, max_per_family.get("default", 2))
        else:
            limit = max_per_family
        family_entries.sort(key=lambda e: (e[1], e[2], e[3]), reverse=True)
        for entry in family_entries[:limit]:
            filtered.append(entry[0])

    logger.info(
        "Filtered %d models to %d latest models (max_per_family=%s): %s",
        len(models),
        len(filtered),
        max_per_family,
        filtered,
    )
    return filtered
