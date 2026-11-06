"""Per-turn metadata that ACP clients receive alongside a prompt response.

Providers may contribute only ``correlationId``. ``model`` always comes from the
agent so a plugin cannot spoof which model answered.
Called by: session.py :: ACPSession.prompt() and agent.py :: CodePuppyAgent.prompt().
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

TurnMetadataProvider = Callable[[Any, str], dict[str, Any] | None]

CORRELATION_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_TURN_KEYS = ("model", "correlationId")

_providers: list[TurnMetadataProvider] = []
_providers_lock = threading.Lock()


def register_turn_metadata_provider(fn: TurnMetadataProvider) -> None:
    with _providers_lock:
        if fn not in _providers:
            _providers.append(fn)


def unregister_turn_metadata_provider(fn: TurnMetadataProvider) -> None:
    with _providers_lock:
        if fn in _providers:
            _providers.remove(fn)


def model_name(agent: Any) -> str | None:
    try:
        name = agent.get_model_name()
    except Exception as exc:
        # Exception type only: messages may carry tokens or other secrets.
        logger.debug("turn metadata model lookup failed: %s", type(exc).__name__)
        return None
    if not isinstance(name, str):
        return None
    return name.strip() or None


def _valid_correlation_id(result: object) -> str | None:
    if not isinstance(result, dict):
        return None
    value = result.get("correlationId")
    if isinstance(value, str) and CORRELATION_ID_RE.fullmatch(value):
        return value
    return None


def collect_turn_metadata(agent: Any, session_id: str) -> dict[str, str]:
    values: dict[str, str] = {}
    model = model_name(agent)
    if model is not None:
        values["model"] = model

    with _providers_lock:
        providers = list(_providers)
    for provider in providers:
        try:
            correlation_id = _valid_correlation_id(provider(agent, session_id))
        except Exception as exc:
            # Exception type only: messages may carry tokens or other secrets.
            logger.debug("turn metadata provider failed: %s", type(exc).__name__)
            continue
        if correlation_id is not None and "correlationId" not in values:
            values["correlationId"] = correlation_id
    return values


def turn_meta(values: Mapping[str, str] | None) -> dict[str, Any] | None:
    if not values:
        return None
    turn = {key: values[key] for key in _TURN_KEYS if key in values}
    if not turn:
        return None
    return {"codePuppy": {"turn": turn}}
