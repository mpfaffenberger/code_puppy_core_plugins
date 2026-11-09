"""The ``pstack`` setting: off unless the user turns it on.

pstack is an opinionated skill pack with an always-on routing prompt, so the
bundle ships it dark. ``/pstack on`` (or ``/set pstack=true``) lights up every
hook; ``/pstack`` itself always answers so the pack stays discoverable.
"""

from __future__ import annotations

from code_puppy.command_line.set_menu_schema import Setting, SettingsCategory
from code_puppy.config import get_truthy_bool_value, set_value

ENABLED_KEY = "pstack"


def is_enabled() -> bool:
    return get_truthy_bool_value(ENABLED_KEY, False)


def set_enabled(enabled: bool) -> None:
    set_value(ENABLED_KEY, "true" if enabled else "false")


SETTINGS = SettingsCategory(
    "pstack",
    (
        Setting(
            key=ENABLED_KEY,
            display_name="pstack skill pack",
            description=(
                "poteto's pstack: workflow skills, engineering principles and a "
                "routing prompt. Off by default; /pstack shows what it adds."
            ),
            type_hint="bool",
            effective_getter=is_enabled,
        ),
    ),
)
