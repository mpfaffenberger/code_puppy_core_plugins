"""OpenRouter OAuth plugin callbacks.

Hooks into the ``/add_model`` credential step: when the user adds an
OpenRouter model and ``OPENROUTER_API_KEY`` is not set, offer the one-click
browser PKCE sign-in before falling back to manual key entry.
"""

from __future__ import annotations

import logging
from typing import Optional

from code_puppy.callbacks import register_callback

from .config import OPENROUTER_OAUTH_CONFIG

logger = logging.getLogger(__name__)

_OAUTH = "oauth"
_MANUAL = "manual"


def _themed(builder):
    """Apply the app menu theme when the host exposes it."""
    try:
        from code_puppy.command_line.tui_style import themed
    except Exception:  # noqa: BLE001 - older cores; unthemed menus still work
        return builder
    return themed(builder)


def build_choice_menu(**overrides):
    """Menu offering browser sign-in vs manual key entry."""
    from termflow.tui import MenuBuilder, MenuItem

    env_var = OPENROUTER_OAUTH_CONFIG["env_var"]
    builder = _themed(
        MenuBuilder(f"OpenRouter needs {env_var}")
        .items(
            [
                MenuItem(
                    "Sign in with OpenRouter (browser)",
                    value=_OAUTH,
                    description="OAuth PKCE — mints a key for you",
                ),
                MenuItem(
                    "Paste an API key manually",
                    value=_MANUAL,
                    description="Get one at https://openrouter.ai/keys",
                ),
            ]
        )
        .alt_screen(False)
        .footer_hint("Enter select - Esc enter key manually")
    )
    for name, value in overrides.items():
        getattr(builder, name)(value)
    return builder.build()


def choose_flow(**overrides) -> str:
    """Ask how to acquire the key. Esc/cancel defers to manual entry."""
    result = build_choice_menu(**overrides).run()
    if result.cancelled or result.item is None:
        return _MANUAL
    return result.item.value


def _credential_flow(*, provider_id: str, env_var: str) -> Optional[bool]:
    """``provider_credential_flow`` hook: OpenRouter browser sign-in.

    Returns True when a key was obtained and saved; None to defer to the
    core manual-entry prompt (wrong provider, user preference, or a failed
    OAuth attempt).
    """
    if (
        provider_id != OPENROUTER_OAUTH_CONFIG["provider_id"]
        or env_var != OPENROUTER_OAUTH_CONFIG["env_var"]
    ):
        return None
    if choose_flow() != _OAUTH:
        return None

    from .oauth_flow import run_oauth_flow

    return True if run_oauth_flow() else None


try:
    register_callback("provider_credential_flow", _credential_flow)
except ValueError:
    # Older core without the hook: stay quiet, /add_model still works manually.
    logger.debug("Core lacks provider_credential_flow; openrouter_oauth inactive")
