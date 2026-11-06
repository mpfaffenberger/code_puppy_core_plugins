"""Opt-in Logfire instrumentation of pydantic-ai agent runs.

Dormant unless the user opts in with ``/set enable_logfire true`` or a truthy
``CODE_PUPPY_ENABLE_LOGFIRE``: until then nothing imports the SDK, warns, or
touches the environment. Even when opted in, nothing leaves the machine
without a write token (``send_to_logfire="if-token-present"``). Every failure
degrades to "no telemetry", never to a broken CLI.
"""

from __future__ import annotations

import os
from typing import Any

from . import legacy_core
from .sdk import MISSING_SDK_HINT, sdk_available

ENABLE_KEY = "enable_logfire"
ENABLE_ENV = "CODE_PUPPY_ENABLE_LOGFIRE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_active = False
_agent_contexts: dict[str, Any] = {}


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def opted_in() -> bool:
    """A truthy env var opts in without touching ``puppy.cfg``."""
    if _truthy(os.environ.get(ENABLE_ENV)):
        return True
    from code_puppy.config import get_value

    return _truthy(get_value(ENABLE_KEY))


def resolve_token() -> str | None:
    """Prefer the user's own ``LOGFIRE_TOKEN``, then the onboarded project's."""
    token = os.environ.get(legacy_core.TOKEN_ENV)
    if token:
        return token
    from ..logfire_oauth.oauth import load_project_credentials

    credentials = load_project_credentials()
    return credentials.token if credentials else None


def configure(token: str | None = None) -> bool:
    """Configure Logfire when opted in; return whether instrumentation is live.

    An explicit ``token`` (e.g. one onboarding just minted) beats
    ``resolve_token()``. Safe to call again: the SDK is reconfigured in place.
    """
    global _active
    _active = False

    if not opted_in():
        return False

    from code_puppy.messaging import emit_system_message, emit_warning

    legacy = legacy_core.loaded()
    if legacy is not None:
        legacy_core.export_token(token or resolve_token(), overwrite=True)
        return bool(legacy.configure_logfire())

    if not sdk_available():
        emit_warning(MISSING_SDK_HINT)
        return False

    try:
        import logfire

        logfire.configure(
            service_name="code-puppy",
            token=token or resolve_token(),
            send_to_logfire="if-token-present",
            console=False,
        )
        logfire.instrument_pydantic_ai()
    except Exception as exc:
        emit_warning(f"Failed to configure Logfire: {exc}")
        return False

    _active = True
    emit_system_message(
        "Logfire instrumentation enabled - spans ship only if a Logfire "
        "token is configured."
    )
    return True


def capture_context(group_id: str) -> None:
    """Remember the live agent span so a later cancellation can nest under it."""
    if not _active:
        return
    try:
        import logfire

        _agent_contexts[group_id] = logfire.get_context()
    except Exception:
        return


def clear_context(group_id: str) -> None:
    """Discard trace context retained for a finished agent run."""
    _agent_contexts.pop(group_id, None)


def emit_cancellation(group_id: str) -> None:
    """Log a cancellation as a child of its instrumented agent run."""
    if not _active:
        return
    context = _agent_contexts.pop(group_id, None)
    try:
        import logfire

        if context is None:
            logfire.warning("Agent run cancelled", group_id=group_id)
            return
        with logfire.attach_context(context):
            logfire.warning("Agent run cancelled", group_id=group_id)
    except Exception:
        return
