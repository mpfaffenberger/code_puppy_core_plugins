"""`/set` knobs for gemini_no_thinking_guard, read fresh on every call (no restart)."""

from __future__ import annotations

from code_puppy.command_line.set_menu_schema import Setting, SettingsCategory
from code_puppy.config import get_value

from .policy import GuardSettings

ENABLED_KEY = "gemini_no_thinking_guard"
STEP_BACK_AFTER_KEY = "gemini_no_thinking_guard_step_back_after"
FORCE_ANSWER_AFTER_KEY = "gemini_no_thinking_guard_force_answer_after"

_DEFAULTS = GuardSettings()
_FALSY = ("false", "0", "no", "off")


def is_enabled() -> bool:
    """On unless explicitly ``false/0/no/off``."""
    value = get_value(ENABLED_KEY)
    if value is None or not str(value).strip():
        return True
    return str(value).strip().lower() not in _FALSY


def is_gemini_flash(model_name: str | None) -> bool:
    name = (model_name or "").lower()
    return "gemini" in name and "flash" in name


def guard_settings() -> GuardSettings:
    return GuardSettings(
        step_back_after=_count(STEP_BACK_AFTER_KEY, _DEFAULTS.step_back_after),
        force_answer_after=_count(FORCE_ANSWER_AFTER_KEY, _DEFAULTS.force_answer_after),
    )


def _count(key: str, default: int) -> int:
    """Non-negative int setting; anything unparseable keeps the default."""
    try:
        value = int(str(get_value(key)).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


SETTINGS = SettingsCategory(
    "Gemini No-Thinking Guard",
    (
        Setting(
            key=ENABLED_KEY,
            display_name="Gemini No-Thinking Guard",
            description=(
                "On Gemini Flash turns where the model did not think, make it step "
                "back and then answer instead of looping on lookups. Default on."
            ),
            type_hint="bool",
            effective_getter=is_enabled,
        ),
        Setting(
            key=STEP_BACK_AFTER_KEY,
            display_name="No-Thinking Guard: Step Back After",
            description=(
                "Tool calls since the last edit before one call is replaced by a "
                "step-back instruction. 0 disables. Default 12."
            ),
            type_hint="int",
            effective_getter=lambda: guard_settings().step_back_after,
        ),
        Setting(
            key=FORCE_ANSWER_AFTER_KEY,
            display_name="No-Thinking Guard: Force Answer After",
            description=(
                "Tool calls since the last edit after which tools are blocked and "
                "the model must answer. 0 disables. Default 24."
            ),
            type_hint="int",
            effective_getter=lambda: guard_settings().force_answer_after,
        ),
    ),
)
