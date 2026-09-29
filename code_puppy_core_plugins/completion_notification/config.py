"""Configuration accessors for the completion-notification plugin."""

from __future__ import annotations

from code_puppy.config import get_value

KEY_ENABLED = "completion_notifications"
KEY_SOUND = "completion_notification_sound"
_TRUTHY = {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    """Return whether completion notifications are explicitly enabled."""
    return str(get_value(KEY_ENABLED) or "").strip().lower() in _TRUTHY


def get_sound() -> str:
    """Return an optional named macOS sound or absolute local sound-file path."""
    return str(get_value(KEY_SOUND) or "").strip()
