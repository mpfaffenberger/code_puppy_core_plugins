"""Unit tests for the ACP neutral auth-status registry and its meta projection."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from code_puppy_core_plugins.acp import auth_status
from code_puppy_core_plugins.acp.auth_status import (
    AuthStatusReport,
    auth_methods,
    auth_status_for,
    auth_status_meta,
    current_auth_reports,
    register_auth_status_provider,
    unregister_auth_status_provider,
)
from tests.acp_auth_status_support import clean_registry  # noqa: F401

pytestmark = pytest.mark.usefixtures("clean_registry")

SECRET = "SECRET-TOKEN-xyz"


def _report(**overrides) -> AuthStatusReport:
    values = {"method_id": "m1", "name": "Method One", "status": "signed_in"}
    values.update(overrides)
    return AuthStatusReport(**values)


def _returning(value):
    def provider():
        return value

    return provider


@pytest.mark.parametrize("state", ["signed_in", "expired", "missing", "not_applicable"])
def test_meta_without_expiry_or_user_has_only_status(state):
    assert auth_status_meta(_report(status=state)) == {
        "codePuppy": {"authStatus": {"status": state}}
    }


@pytest.mark.parametrize(
    ("overrides", "expected_inner"),
    [
        (
            {"expires_at": datetime(2030, 1, 2, 3, 4, 5, 999)},
            {"status": "signed_in", "expiresAt": "2030-01-02T03:04:05Z"},
        ),
        (
            {
                "expires_at": datetime(
                    2030, 1, 2, 5, 4, 5, tzinfo=timezone(timedelta(hours=2))
                )
            },
            {"status": "signed_in", "expiresAt": "2030-01-02T03:04:05Z"},
        ),
        ({"user_id": "  wmt123  "}, {"status": "signed_in", "userId": "wmt123"}),
        ({"user_id": "x" * 300}, {"status": "signed_in", "userId": "x" * 128}),
        ({"user_id": "   "}, {"status": "signed_in"}),
        ({"user_id": None}, {"status": "signed_in"}),
    ],
)
def test_meta_projects_expiry_and_user_id(overrides, expected_inner):
    meta = auth_status_meta(_report(**overrides))

    assert meta == {"codePuppy": {"authStatus": expected_inner}}


@dataclass(frozen=True)
class _LeakyReport(AuthStatusReport):
    token: str = SECRET


def test_meta_never_copies_subclass_fields():
    report = _LeakyReport(
        method_id="m1",
        name="Leaky",
        status="signed_in",
        expires_at=datetime(2030, 1, 2, 3, 4, 5),
        user_id="wmt123",
    )

    meta = auth_status_meta(report)

    assert set(meta["codePuppy"]["authStatus"]) == {"status", "expiresAt", "userId"}
    assert SECRET not in json.dumps(meta)


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {"method_id": "m1", "name": "Method One"},
        _report(status="bogus"),
        _report(method_id=""),
        _report(method_id="   "),
        _report(name=""),
    ],
    ids=[
        "none",
        "dict",
        "bogus-status",
        "empty-method-id",
        "blank-method-id",
        "empty-name",
    ],
)
def test_invalid_provider_output_is_skipped(invalid):
    register_auth_status_provider(_returning(invalid))

    assert current_auth_reports() == []
    assert auth_methods() is None


def test_empty_registry_yields_no_reports_and_no_methods():
    assert current_auth_reports() == []
    assert auth_methods() is None


def test_raising_provider_is_skipped_without_leaking_message(caplog):
    def leaky():
        raise RuntimeError(SECRET)

    good = _report(method_id="ok")
    register_auth_status_provider(leaky)
    register_auth_status_provider(_returning(good))

    with caplog.at_level(logging.DEBUG, logger=auth_status.logger.name):
        reports = current_auth_reports()

    assert reports == [good]
    assert "RuntimeError" in caplog.text
    assert SECRET not in caplog.text


def test_registering_same_provider_twice_yields_one_report():
    provider = _returning(_report())
    register_auth_status_provider(provider)
    register_auth_status_provider(provider)

    assert len(current_auth_reports()) == 1


def test_unregister_removes_provider_and_absent_unregister_is_noop():
    provider = _returning(_report())
    register_auth_status_provider(provider)

    unregister_auth_status_provider(provider)
    unregister_auth_status_provider(provider)

    assert current_auth_reports() == []


def test_first_provider_wins_for_duplicate_method_id():
    first = _report(name="First")
    second = _report(name="Second")
    register_auth_status_provider(_returning(first))
    register_auth_status_provider(_returning(second))

    assert current_auth_reports() == [first]


def test_auth_status_for_returns_match_or_none():
    report = _report(method_id="wanted")
    register_auth_status_provider(_returning(report))

    assert auth_status_for("wanted") is report
    assert auth_status_for("unknown") is None


def test_auth_methods_projects_each_report():
    report = _report(
        method_id="m1",
        name="Method One",
        description="Signs in",
        status="expired",
        user_id="wmt123",
    )
    register_auth_status_provider(_returning(report))

    [method] = auth_methods()

    assert method.id == "m1"
    assert method.name == "Method One"
    assert method.description == "Signs in"
    assert method.field_meta == auth_status_meta(report)
