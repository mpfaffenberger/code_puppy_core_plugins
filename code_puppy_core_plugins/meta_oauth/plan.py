"""Describe the Muse Code subscription plan carried by a minted Meta key.

Meta's ``/muse-code/key`` response includes plan fields (``subs_tier_name``,
``is_subs_active``, ``subs_usage``...). They are a snapshot taken at login, so
usage figures are labelled as such rather than presented as live numbers.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from code_puppy.i18n import t


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _subscription_active(credentials: Mapping[str, object]) -> bool | None:
    # The mint response says ``is_subs_active``; the Muse CLI file says ``subs_active``.
    for key in ("is_subs_active", "subs_active"):
        value = credentials.get(key)
        if isinstance(value, bool):
            return value
    return None


def _format_reset(value: object) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    return str(value) if value else ""


def _usage_line(label: str, snapshot: Mapping[str, object]) -> str | None:
    used = snapshot.get("used_percent")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    reset = _format_reset(snapshot.get("resets_at"))
    if reset:
        return t(
            "plugin.meta-oauth.plan.usage_reset",
            window=label,
            used=f"{used:g}",
            reset=reset,
        )
    return t("plugin.meta-oauth.plan.usage", window=label, used=f"{used:g}")


def _window_label(snapshot: Mapping[str, object]) -> str:
    minutes = snapshot.get("window_duration_mins")
    if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes > 0:
        if minutes % 60 == 0:
            return t("plugin.meta-oauth.plan.window_hours", hours=minutes // 60)
        return t("plugin.meta-oauth.plan.window_minutes", minutes=minutes)
    return t("plugin.meta-oauth.plan.window")


def describe_plan(credentials: Mapping[str, object]) -> list[str]:
    """Return human-readable plan lines; empty when Meta sent no plan data."""
    active = _subscription_active(credentials)
    tier = credentials.get("subs_tier_name") or credentials.get("subs_tier_id")
    lines: list[str] = []
    if tier:
        lines.append(t("plugin.meta-oauth.plan.tier", tier=tier))
    if active is not None:
        lines.append(
            t(
                "plugin.meta-oauth.plan.active"
                if active
                else "plugin.meta-oauth.plan.inactive"
            )
        )

    usage = _mapping(credentials.get("subs_usage"))
    window = _mapping(usage.get("window"))
    candidates = (
        _usage_line(_window_label(window), window),
        _usage_line(t("plugin.meta-oauth.plan.weekly"), _mapping(usage.get("weekly"))),
    )
    lines.extend(line for line in candidates if line)

    if credentials.get("is_subs_upgrade_available") is True:
        lines.append(t("plugin.meta-oauth.plan.upgrade"))
    return lines
