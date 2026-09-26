"""Configuration accessors for the headroom_compression plugin."""

from __future__ import annotations

from urllib.parse import urlsplit

from code_puppy.config import get_truthy_bool_value, get_value, set_value

KEY_ENABLED = "headroom_compression"
KEY_UPSTREAM_URL = "headroom_upstream_url"


def is_enabled() -> bool:
    """Return whether headroom compression is explicitly enabled."""
    return get_truthy_bool_value(KEY_ENABLED, False)


def get_upstream_url() -> str:
    """Return the real endpoint URL headroom should proxy to, or ''."""
    return str(get_value(KEY_UPSTREAM_URL) or "").strip()


def enable(upstream_url: str) -> bool:
    """Turn headroom compression on for ``upstream_url``.

    Returns False (and does not enable) if ``upstream_url`` isn't a valid
    absolute http(s) URL -- an invalid upstream can never be matched by
    resolve_custom_endpoint_url anyway, and letting it through silently
    would just leave compression permanently inert.
    """
    parts = urlsplit(upstream_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    set_value(KEY_UPSTREAM_URL, upstream_url)
    set_value(KEY_ENABLED, "true")
    return True


def disable() -> None:
    """Turn headroom compression off (keeps the last upstream URL on disk)."""
    set_value(KEY_ENABLED, "false")
