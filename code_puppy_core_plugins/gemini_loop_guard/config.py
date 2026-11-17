"""`/set` knobs for gemini_loop_guard, read fresh on every call (no restart)."""

from __future__ import annotations

from code_puppy.command_line.set_menu_schema import Setting, SettingsCategory
from code_puppy.config import get_value

from .policy import GuardSettings

ENABLED_KEY = "gemini_loop_guard"
REFLECT_AFTER_KEY = "gemini_loop_guard_reflect_after"
DEDUPE_KEY = "gemini_loop_guard_dedupe"
BLOCK_AFTER_KEY = "gemini_loop_guard_block_after"
VARIATION_AFTER_KEY = "gemini_loop_guard_variation_after"

_DEFAULTS = GuardSettings()
_FALSY = ("false", "0", "no", "off")


def is_enabled() -> bool:
    """On unless explicitly ``false/0/no/off``."""
    return _flag(ENABLED_KEY, default=True)


def is_gemini_model(model_name: str | None) -> bool:
    return bool(model_name) and "gemini" in model_name.lower()


def guard_settings() -> GuardSettings:
    return GuardSettings(
        reflect_after=_count(REFLECT_AFTER_KEY, _DEFAULTS.reflect_after),
        dedupe=_flag(DEDUPE_KEY, default=_DEFAULTS.dedupe),
        block_after=_count(BLOCK_AFTER_KEY, _DEFAULTS.block_after),
        variation_after=_count(VARIATION_AFTER_KEY, _DEFAULTS.variation_after),
    )


def _flag(key: str, *, default: bool) -> bool:
    value = get_value(key)
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() not in _FALSY


def _count(key: str, default: int) -> int:
    """Non-negative int setting; anything unparseable keeps the default."""
    try:
        value = int(str(get_value(key)).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


SETTINGS = SettingsCategory(
    "Gemini Loop Guard",
    (
        Setting(
            key=ENABLED_KEY,
            display_name="Gemini Loop Guard",
            description=(
                "Watch Gemini tool calls and step in when several in a row "
                "find nothing new. Default on."
            ),
            type_hint="bool",
            effective_getter=is_enabled,
        ),
        Setting(
            key=REFLECT_AFTER_KEY,
            display_name="Loop Guard: Reflect After",
            description=(
                "Unproductive calls in a row before the model is shown what it "
                "tried and told it may stop. 0 disables. Default 3."
            ),
            type_hint="int",
            effective_getter=lambda: guard_settings().reflect_after,
        ),
        Setting(
            key=DEDUPE_KEY,
            display_name="Loop Guard: Skip Exact Repeats",
            description=(
                "Skip a call identical to one in the current unproductive "
                "streak; it would return the same thing. Default on."
            ),
            type_hint="bool",
            effective_getter=lambda: guard_settings().dedupe,
        ),
        Setting(
            key=BLOCK_AFTER_KEY,
            display_name="Loop Guard: Block After",
            description=(
                "Last resort: block tools after this many unproductive calls "
                "in a row so the model must answer. 0 (default) disables."
            ),
            type_hint="int",
            effective_getter=lambda: guard_settings().block_after,
        ),
        Setting(
            key=VARIATION_AFTER_KEY,
            display_name="Loop Guard: Variation After",
            description=(
                "Near-identical search/shell calls in a row before the model is "
                "asked to check it is still making progress, even when each "
                "call returns something. 0 disables. Default 3."
            ),
            type_hint="int",
            effective_getter=lambda: guard_settings().variation_after,
        ),
    ),
)
