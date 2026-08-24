from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from code_puppy_core_plugins.logfire_oauth import query_tool
from code_puppy_core_plugins.logfire_oauth.oauth import OAuthTokens


class FakeAgent:
    def __init__(self) -> None:
        self.registered: Any = None

    def tool(self, function):
        self.registered = function
        return function


@pytest.mark.asyncio
async def test_query_requires_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = FakeAgent()
    monkeypatch.setattr(query_tool, "load_tokens", lambda: None)
    query_tool.register_logfire_query(agent)

    result = await agent.registered(None, "SELECT 1")

    assert result.error == "Logfire is not authenticated. Run /logfire auth first."


@pytest.mark.asyncio
async def test_query_forwards_bounded_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = FakeAgent()
    tokens = OAuthTokens(
        access_token="secret",
        refresh_token="refresh",
        token_type="Bearer",
        expires_at=time.time() + 600,
        scope="project:read",
        base_url="https://logfire-us.pydantic.dev",
        client_id="client",
    )
    call = AsyncMock(return_value={"rows": [{"answer": 42}]})
    monkeypatch.setattr(query_tool, "load_tokens", lambda: tokens)
    monkeypatch.setattr(query_tool, "_query_mcp", call)
    query_tool.register_logfire_query(agent)

    result = await agent.registered(
        None,
        " SELECT 42 LIMIT 1 ",
        project="puppy",
        start="2026-08-20T00:00:00Z",
        end="2026-08-21T00:00:00Z",
    )

    assert result.error is None
    assert result.result == {"rows": [{"answer": 42}]}
    call.assert_awaited_once_with(
        base_url="https://logfire-us.pydantic.dev",
        access_token="secret",
        query="SELECT 42 LIMIT 1",
        project="puppy",
        start="2026-08-20T00:00:00Z",
        end="2026-08-21T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_query_rejects_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = FakeAgent()
    tokens = OAuthTokens(
        access_token="secret",
        refresh_token=None,
        token_type="Bearer",
        expires_at=0,
        scope="project:read",
        base_url="https://example.com",
        client_id="client",
    )
    monkeypatch.setattr(query_tool, "load_tokens", lambda: tokens)
    query_tool.register_logfire_query(agent)

    result = await agent.registered(None, "SELECT 1")

    assert "expired" in result.error
