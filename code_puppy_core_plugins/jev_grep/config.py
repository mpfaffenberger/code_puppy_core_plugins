"""Settings for smart_grep. Set with `/set <key> <value>` or the environment.

/set smart_grep on          enable (off by default)
/set jev_api_key <key>      credentials (or TYPESAFE_API_KEY)
/set smart_grep_model ...   pin a Jev version, e.g. jev-1.13.0
/set smart_grep_threshold   relevance cut-off, 0-1 (default 0.5)
"""

from __future__ import annotations

import os

from code_puppy.config import get_api_key, get_truthy_bool_value, get_value

ENABLED_KEY = "smart_grep"
MODEL_KEY = "smart_grep_model"
THRESHOLD_KEY = "smart_grep_threshold"
# What `/set` autocompletes. The API key is deliberately absent: completions
# echo the current value, and secrets don't belong in a popup.
SETTING_KEYS = (ENABLED_KEY, MODEL_KEY, THRESHOLD_KEY)
# TypeSafe's official name first, then the Jev-branded alias. Config lookup
# is case-insensitive, so `/set jev_api_key ...` matches JEV_API_KEY.
API_KEY_NAMES = ("TYPESAFE_API_KEY", "JEV_API_KEY")
API_KEY_NAME = API_KEY_NAMES[0]
DEFAULT_MODEL = "jev-latest"
DEFAULT_THRESHOLD = 0.5


def get_typesafe_api_key() -> str:
    """First key found: environment beats config, official name beats alias."""
    for name in API_KEY_NAMES:
        if key := os.environ.get(name):
            return key
    for name in API_KEY_NAMES:
        if key := get_api_key(name):
            return key
    return ""


def is_enabled() -> bool:
    """Opt-in: smart_grep sends selected source to an external service."""
    return get_truthy_bool_value(ENABLED_KEY, False)


def is_available() -> bool:
    """The single gate for tool exposure, prompting and execution."""
    return is_enabled() and bool(get_typesafe_api_key())


def get_jev_model_name() -> str:
    """Pin a version (e.g. ``jev-1.13.0``) once a threshold is tuned against it."""
    return get_value(MODEL_KEY) or DEFAULT_MODEL


def get_threshold() -> float:
    try:
        value = float(get_value(THRESHOLD_KEY) or DEFAULT_THRESHOLD)
    except ValueError:
        return DEFAULT_THRESHOLD
    return value if 0 <= value <= 1 else DEFAULT_THRESHOLD
