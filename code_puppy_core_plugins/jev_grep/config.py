"""Settings for smart_grep. Set with `/set <key> <value>` or the environment.

Declared once in :data:`SETTINGS`, which feeds `/set` autocomplete and the
`/set` menu via core's ``register_settings`` hook. The credential can also
come from the TYPESAFE_API_KEY / JEV_API_KEY environment variables.
"""

from __future__ import annotations

import os

from code_puppy.command_line.set_menu_schema import Setting, SettingsCategory
from code_puppy.config import get_api_key, get_truthy_bool_value, get_value

ENABLED_KEY = "smart_grep"
MODEL_KEY = "smart_grep_model"
THRESHOLD_KEY = "smart_grep_threshold"
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


# One declaration for `/set` autocomplete and the `/set` menu. The credential
# joins core's "API Keys" section, where it is masked and never echoed.
SETTINGS = [
    SettingsCategory(
        "Smart Grep",
        (
            Setting(
                key=ENABLED_KEY,
                display_name="Smart Grep",
                description=(
                    "Semantic code search judged by TypeSafe's Jev. Sends selected "
                    "source, paths and the query to TypeSafe; needs an API key."
                ),
                type_hint="bool",
                effective_getter=is_enabled,
            ),
            Setting(
                key=MODEL_KEY,
                display_name="Smart Grep Model",
                description="Jev version to judge with, e.g. jev-1.13.0.",
                type_hint="string",
                effective_getter=get_jev_model_name,
            ),
            Setting(
                key=THRESHOLD_KEY,
                display_name="Smart Grep Threshold",
                description="Relevance cut-off for matches, 0-1.",
                type_hint="float",
                effective_getter=get_threshold,
            ),
        ),
    ),
    SettingsCategory(
        "API Keys",
        (
            Setting(
                key=API_KEY_NAME.lower(),
                display_name="TypeSafe API Key",
                description="Credential for smart_grep (or set TYPESAFE_API_KEY).",
                type_hint="string",
                effective_getter=get_typesafe_api_key,
                sensitive=True,
            ),
        ),
    ),
]
