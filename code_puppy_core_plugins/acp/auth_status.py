"""Neutral registry of sign-in state that plugins report to ACP clients.

Any plugin registers a provider; ACP reads it without knowing who owns it.
Called by: agent.py :: CodePuppyAgent.initialize() / authenticate().
Readers must never trigger login, network I/O, or prompts; providers stay read-only.
"""

from __future__ import annotations

import logging
import threading
import typing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from acp.schema import AuthMethodAgent

logger = logging.getLogger(__name__)

AuthState = Literal["signed_in", "expired", "missing", "not_applicable"]

_USER_ID_MAX_LEN = 128


@dataclass(frozen=True)
class AuthStatusReport:
    method_id: str
    name: str
    status: AuthState
    description: str | None = None
    expires_at: datetime | None = None
    user_id: str | None = None


AuthStatusProvider = Callable[[], "AuthStatusReport | None"]

_providers: list[AuthStatusProvider] = []
_providers_lock = threading.Lock()


def register_auth_status_provider(provider: AuthStatusProvider) -> None:
    with _providers_lock:
        if provider not in _providers:
            _providers.append(provider)


def unregister_auth_status_provider(provider: AuthStatusProvider) -> None:
    with _providers_lock:
        if provider in _providers:
            _providers.remove(provider)


def _is_valid_report(report: object) -> bool:
    if not isinstance(report, AuthStatusReport):
        return False
    if report.status not in typing.get_args(AuthState):
        return False
    for text in (report.method_id, report.name):
        if not isinstance(text, str) or not text.strip():
            return False
    if report.description is not None and not isinstance(report.description, str):
        return False
    if report.expires_at is not None and not isinstance(report.expires_at, datetime):
        return False
    if report.user_id is not None and not isinstance(report.user_id, str):
        return False
    return True


def current_auth_reports() -> list[AuthStatusReport]:
    with _providers_lock:
        providers = list(_providers)

    reports: list[AuthStatusReport] = []
    seen_ids: set[str] = set()
    for provider in providers:
        try:
            report = provider()
        except Exception as exc:
            # Exception type only: messages may carry tokens or other secrets.
            logger.debug("auth status provider failed: %s", type(exc).__name__)
            continue
        if not _is_valid_report(report) or report.method_id in seen_ids:
            continue
        seen_ids.add(report.method_id)
        reports.append(report)
    return reports


def auth_status_for(method_id: str) -> AuthStatusReport | None:
    for report in current_auth_reports():
        if report.method_id == method_id and _meta_or_none(report) is not None:
            return report
    return None


def _format_expires_at(expires_at: datetime) -> str:
    if expires_at.tzinfo is None:
        utc_value = expires_at.replace(tzinfo=timezone.utc)
    else:
        utc_value = expires_at.astimezone(timezone.utc)
    return utc_value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def auth_status_meta(report: AuthStatusReport) -> dict[str, Any]:
    # Allow-list only: copying other fields could leak data a subclass carries.
    inner: dict[str, Any] = {"status": report.status}
    if report.expires_at is not None:
        inner["expiresAt"] = _format_expires_at(report.expires_at)
    if isinstance(report.user_id, str):
        user_id = report.user_id.strip()[:_USER_ID_MAX_LEN]
        if user_id:
            inner["userId"] = user_id
    return {"codePuppy": {"authStatus": inner}}


def _meta_or_none(report: AuthStatusReport) -> dict[str, Any] | None:
    try:
        return auth_status_meta(report)
    except Exception as exc:
        # Exception type only: messages may carry tokens or other secrets.
        logger.debug(
            "auth status report could not be serialized: %s", type(exc).__name__
        )
        return None


def auth_methods() -> list[AuthMethodAgent] | None:
    methods: list[AuthMethodAgent] = []
    for report in current_auth_reports():
        meta = _meta_or_none(report)
        if meta is None:
            continue
        try:
            methods.append(
                AuthMethodAgent(
                    id=report.method_id,
                    name=report.name,
                    description=report.description,
                    field_meta=meta,
                )
            )
        except Exception as exc:
            logger.debug(
                "auth status report could not be serialized: %s", type(exc).__name__
            )
    return methods or None
