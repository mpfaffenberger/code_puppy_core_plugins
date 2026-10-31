"""Wire-level contract for ACP auth status in initialize/authenticate payloads.

Covers agent.py :: CodePuppyAgent.initialize() / authenticate() as serialized by
the SDK, so assertions see exactly what an ACP client receives.
"""

from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from code_puppy_core_plugins.acp import io_delegation, permissions
from code_puppy_core_plugins.acp.agent import CodePuppyAgent
from code_puppy_core_plugins.acp.auth_status import (
    AuthStatusReport,
    register_auth_status_provider,
)
from tests.acp_auth_status_support import clean_registry  # noqa: F401

pytestmark = pytest.mark.usefixtures("clean_registry")

EXPIRES = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
SECRET = "SECRET-TOKEN-xyz"


@dataclass(frozen=True)
class _TokenReport(AuthStatusReport):
    token: str = SECRET


def _wire(model: Any) -> dict[str, Any]:
    return model.model_dump(
        mode="json", by_alias=True, exclude_none=True, exclude_unset=True
    )


def _auth_meta(status: str, **inner: str) -> dict[str, Any]:
    return {"codePuppy": {"authStatus": {"status": status, **inner}}}


def _register(report: AuthStatusReport | None) -> None:
    register_auth_status_provider(lambda: report)


@pytest.fixture
def agent():
    instance = CodePuppyAgent()
    yield instance
    io_delegation.uninstall()
    permissions.uninstall()


async def test_no_provider_omits_auth_methods_and_empty_authenticate(agent):
    wire_init = _wire(await agent.initialize(protocol_version=1))

    assert "authMethods" not in wire_init
    assert "protocolVersion" in wire_init
    assert "agentInfo" in wire_init
    assert _wire(await agent.authenticate("anything")) == {}


@pytest.mark.parametrize("state", ["signed_in", "expired", "missing", "not_applicable"])
async def test_each_state_is_advertised_in_auth_methods(agent, state):
    _register(AuthStatusReport("example-login", "Example Login", state))

    wire_init = _wire(await agent.initialize(protocol_version=1))

    assert wire_init["authMethods"] == [
        {
            "id": "example-login",
            "name": "Example Login",
            "_meta": _auth_meta(state),
        }
    ]


async def test_details_round_trip_through_initialize_and_authenticate(agent):
    _register(
        AuthStatusReport(
            "example-login",
            "Example Login",
            "signed_in",
            description="Sign in",
            expires_at=EXPIRES,
            user_id="jdoe",
        )
    )
    expected_meta = _auth_meta(
        "signed_in", expiresAt="2030-01-02T03:04:05Z", userId="jdoe"
    )

    wire_init = _wire(await agent.initialize(protocol_version=1))
    wire_auth = _wire(await agent.authenticate("example-login"))

    assert wire_init["authMethods"] == [
        {
            "id": "example-login",
            "name": "Example Login",
            "description": "Sign in",
            "_meta": expected_meta,
        }
    ]
    assert wire_auth == {"_meta": expected_meta}


async def test_unknown_method_authenticates_to_empty_payload(agent):
    _register(AuthStatusReport("example-login", "Example Login", "missing"))

    assert _wire(await agent.authenticate("unknown-method")) == {}


async def test_raising_provider_is_omitted_without_breaking_initialize(agent):
    def boom() -> AuthStatusReport:
        raise RuntimeError("provider failure")

    register_auth_status_provider(boom)

    wire_init = _wire(await agent.initialize(protocol_version=1))

    assert "authMethods" not in wire_init
    assert "protocolVersion" in wire_init
    assert "agentInfo" in wire_init


async def test_secrets_never_reach_wire_payloads_or_logs(agent, caplog):
    caplog.set_level(logging.DEBUG)
    _register(
        _TokenReport(
            "example-login", "Example Login", "signed_in", description="Sign in"
        )
    )

    def boom() -> AuthStatusReport:
        raise RuntimeError(SECRET)

    register_auth_status_provider(boom)

    wire_init = _wire(await agent.initialize(protocol_version=1))
    wire_auth = _wire(await agent.authenticate("example-login"))

    assert wire_init["authMethods"][0]["id"] == "example-login"
    for text in (json.dumps(wire_init), json.dumps(wire_auth), caplog.text):
        assert SECRET not in text


async def test_auth_status_never_uses_the_network(agent, monkeypatch):
    def no_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("network used")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    _register(AuthStatusReport("example-login", "Example Login", "signed_in"))

    assert "authMethods" in _wire(await agent.initialize(protocol_version=1))
    assert _wire(await agent.authenticate("example-login"))
    assert _wire(await agent.authenticate("unknown-method")) == {}


async def test_provider_returning_none_matches_no_provider_payload(agent):
    without_provider = _wire(await agent.initialize(protocol_version=1))

    _register(None)
    with_none_provider = _wire(await agent.initialize(protocol_version=1))

    assert with_none_provider == without_provider
