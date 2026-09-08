"""Offline token rotation, atomic persistence, and lifecycle regressions."""

import asyncio
import json
import os
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock

import pytest

from code_puppy_core_plugins.claude_code_oauth import token_store, utils


def _process_refresh(path, calls, results):
    from pathlib import Path

    utils.get_token_storage_path = lambda: Path(path)
    utils.update_claude_code_model_tokens = lambda token: True

    def exchange(*args, **kwargs):
        with calls.get_lock():
            calls.value += 1
        time.sleep(0.05)
        return Mock(
            status_code=200,
            headers={"content-type": "application/json"},
            json=lambda: {
                "access_token": "new",
                "refresh_token": "rotated",
                "expires_in": 3600,
            },
        )

    utils.requests.post = exchange
    results.put(utils.refresh_access_token(force=True, rejected_token="old"))


def test_processes_share_rotation_lock(tokens):
    ctx = multiprocessing.get_context("spawn")
    calls = ctx.Value("i", 0)
    results = ctx.Queue()
    workers = [
        ctx.Process(target=_process_refresh, args=(str(tokens), calls, results))
        for _ in range(3)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0
    assert [results.get(timeout=2) for _ in workers] == ["new"] * 3
    assert calls.value == 1


@pytest.fixture
def tokens(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    monkeypatch.setattr(utils, "get_token_storage_path", lambda: path)
    monkeypatch.setattr(utils, "update_claude_code_model_tokens", Mock())
    assert utils.save_tokens(
        {"access_token": "old", "refresh_token": "refresh", "expires_at": 0}
    )
    return path


def test_concurrent_rejected_token_rotates_only_once(tokens, monkeypatch):
    calls = []

    def exchange(*args, **kwargs):
        calls.append(kwargs["json"]["refresh_token"])
        time.sleep(0.02)
        return Mock(
            status_code=200,
            headers={"content-type": "application/json"},
            json=lambda: {
                "access_token": "new",
                "refresh_token": "rotated",
                "expires_in": 3600,
            },
        )

    monkeypatch.setattr(utils.requests, "post", exchange)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: utils.refresh_access_token(force=True, rejected_token="old"),
                range(8),
            )
        )
    assert results == ["new"] * 8
    assert calls == ["refresh"]
    assert json.loads(tokens.read_text())["refresh_token"] == "rotated"
    if os.name != "nt":
        assert tokens.stat().st_mode & 0o777 == 0o600


def test_failed_atomic_replace_preserves_existing_tokens(tokens, monkeypatch):
    original = tokens.read_bytes()
    monkeypatch.setattr(token_store.os, "replace", Mock(side_effect=OSError))
    assert not utils.save_tokens({"access_token": "replacement"})
    assert tokens.read_bytes() == original
    assert not list(tokens.parent.glob(".claude-token-*"))


def test_invalid_refresh_response_preserves_credentials(tokens, monkeypatch):
    original = tokens.read_bytes()
    monkeypatch.setattr(
        utils.requests,
        "post",
        Mock(
            return_value=Mock(
                status_code=200,
                headers={"content-type": "application/json"},
                json=lambda: {"refresh_token": "rotated-without-access-token"},
            )
        ),
    )
    assert utils.refresh_access_token() is None
    assert tokens.read_bytes() == original


def test_failed_exchange_backs_off_but_forced_refresh_still_rotates(
    tokens, monkeypatch
):
    monkeypatch.setattr(token_store, "_exchange_blocked_until", 0.0)
    responses = iter(
        [
            Mock(status_code=429, headers={"Retry-After": "45"}),
            Mock(
                status_code=200,
                headers={"content-type": "application/json"},
                json=lambda: {"access_token": "new", "expires_in": 3600},
            ),
        ]
    )
    post = Mock(side_effect=lambda *a, **k: next(responses))
    monkeypatch.setattr(utils.requests, "post", post)

    # Expired-in-buffer token: first proactive attempt hits the endpoint and
    # fails; the next ones must not touch the endpoint at all.
    assert utils.refresh_access_token() is None
    assert utils.refresh_access_token() is None
    assert utils.refresh_access_token() is None
    assert post.call_count == 1
    remaining = token_store._exchange_blocked_until - time.monotonic()
    assert 40 < remaining <= 45

    # A rejected token (401 recovery) must still rotate despite the backoff.
    assert utils.refresh_access_token(force=True, rejected_token="old") == "new"
    assert post.call_count == 2


@pytest.mark.asyncio
async def test_hard_429_is_not_multiplied_by_sdk_retries(monkeypatch):
    """Transport retries alone; the SDK must not wrap them 3x (6 -> 18 hits)."""
    import httpx2
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models import ModelRequestParameters

    from code_puppy import claude_cache_client
    from code_puppy_core_plugins.claude_code_oauth import model_provider

    hits = []

    def transport(request):
        hits.append(request.url.path)
        return httpx2.Response(
            429, json={"error": {"type": "rate_limit_error", "message": "Error"}}
        )

    real_client = claude_cache_client.ClaudeCacheAsyncClient

    def with_mock_transport(*args, **kwargs):
        kwargs["transport"] = httpx2.MockTransport(transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(
        claude_cache_client, "ClaudeCacheAsyncClient", with_mock_transport
    )
    monkeypatch.setattr(claude_cache_client.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(model_provider, "emit_warning", Mock())
    monkeypatch.setattr(
        "code_puppy.model_factory.get_custom_config",
        lambda cfg: ("https://api.anthropic.com", {}, None, "tok", None),
    )
    monkeypatch.setattr(
        "code_puppy_core_plugins.claude_code_oauth.register_callbacks.get_valid_access_token",
        lambda: "tok",
    )

    model = model_provider.create_claude_code_model(
        "claude-code-claude-sonnet-5",
        {
            "name": "claude-sonnet-5",
            "oauth_source": "claude-code-plugin",
            "custom_endpoint": {"url": "https://api.anthropic.com"},
            "context_length": 200000,
        },
        {},
    )
    with pytest.raises(Exception) as excinfo:
        await model.request(
            [ModelRequest(parts=[UserPromptPart("hi")])],
            None,
            ModelRequestParameters(),
        )
    assert "429" in str(excinfo.value) or "rate_limit" in str(excinfo.value)
    assert len(hits) == claude_cache_client.MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_runtime_providers_offload_blocking_io(monkeypatch):
    from code_puppy_core_plugins.claude_code_oauth import runtime_credentials

    loop_thread = threading.get_ident()
    threads = []

    def token(*args, **kwargs):
        threads.append(threading.get_ident())
        return "valid"

    monkeypatch.setattr(utils, "get_valid_access_token", token)
    monkeypatch.setattr(utils, "refresh_access_token", token)
    assert await runtime_credentials.current_token() == "valid"
    assert await runtime_credentials.refresh_token(rejected_token="old") == "valid"
    assert all(thread != loop_thread for thread in threads)


@pytest.mark.asyncio
async def test_overlapping_heartbeat_runs_are_reference_counted(monkeypatch):
    from code_puppy_core_plugins.claude_code_oauth import register_callbacks as hooks
    from code_puppy_core_plugins.claude_code_oauth import token_refresh_heartbeat

    heartbeat = Mock(start=AsyncMock(), stop=AsyncMock(), refresh_count=0)
    factory = Mock(return_value=heartbeat)
    monkeypatch.setattr(token_refresh_heartbeat, "TokenRefreshHeartbeat", factory)
    monkeypatch.setattr(hooks, "_active_heartbeats", {})
    await asyncio.gather(
        *(hooks._on_agent_run_start("a", "claude-code-x", "s") for _ in range(2))
    )
    factory.assert_called_once()
    await hooks._on_agent_run_end("a", "claude-code-x", "s")
    heartbeat.stop.assert_not_called()
    await hooks._on_agent_run_end("a", "claude-code-x", "s")
    heartbeat.stop.assert_awaited_once()
    assert not hooks._active_heartbeats
