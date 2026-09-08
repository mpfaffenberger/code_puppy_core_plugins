"""Claude model generation filtering."""

import logging
import re
from typing import Dict, List, Tuple, Union


logger = logging.getLogger(__name__)


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
        if model_name == "claude-opus-5":
            family_models.setdefault("opus", []).append((model_name, 5, 0, 0))
            continue
        if model_name == "claude-opus-4-8":
            family_models.setdefault("opus", []).append((model_name, 4, 8, 20250301))
            continue
        if model_name == "claude-opus-4-7":
            family_models.setdefault("opus", []).append((model_name, 4, 7, 20250219))
            continue
        if model_name == "claude-opus-4-6":
            family_models.setdefault("opus", []).append((model_name, 4, 6, 20260205))
            continue
        if model_name == "claude-sonnet-4-6":
            family_models.setdefault("sonnet", []).append((model_name, 4, 6, 20250610))
            continue
        if model_name == "claude-sonnet-5":
            family_models.setdefault("sonnet", []).append((model_name, 5, 0, 0))
            continue
        if model_name == "claude-fable-5":
            family_models.setdefault("fable", []).append((model_name, 5, 0, 0))
            continue
        if model_name == "claude-fable-5-1":
            family_models.setdefault("fable", []).append((model_name, 5, 1, 0))
            continue
        # Match pattern: claude-{family}-{major}-{minor}-{date}
        # Examples: claude-haiku-3-5-20241022, claude-sonnet-4-5-20250929
        match = re.match(r"claude-(haiku|sonnet|opus)-(\d+)-(\d+)-(\d+)", model_name)
        if not match:
            # Also try pattern with dots: claude-{family}-{major}.{minor}-{date}
            match = re.match(
                r"claude-(haiku|sonnet|opus)-(\d+)\.(\d+)-(\d+)", model_name
            )

        if not match:
            continue

        family = match.group(1)
        major = int(match.group(2))
        minor = int(match.group(3))
        date = int(match.group(4))

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
