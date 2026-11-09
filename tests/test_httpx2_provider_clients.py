"""Regression tests: provider-bound plugin clients must be httpx2.

pydantic-ai deprecates caller-owned legacy ``httpx.AsyncClient`` on
OpenAI-compatible providers (removed in v3). Meta, Grok, and Ollama were
still importing ``create_async_client`` from ``code_puppy.http_utils``
(legacy httpx) instead of ``code_puppy.httpx2_utils``, so building any of
those models emitted ``PydanticAIDeprecationWarning``.
"""

import asyncio
import warnings
from collections.abc import Callable
from unittest.mock import patch

import httpx
import httpx2
from pydantic_ai.exceptions import PydanticAIDeprecationWarning

from code_puppy.httpx2_utils import create_async_client as real_httpx2_factory
from code_puppy_core_plugins.grok_oauth.register_callbacks import (
    _create_grok_oauth_model,
)
from code_puppy_core_plugins.meta_oauth.register_callbacks import (
    _create_meta_oauth_model,
)
from code_puppy_core_plugins.ollama.register_callbacks import create_ollama_model

CreateClient = Callable[..., httpx2.AsyncClient]


def _recording_factory(
    created: list[httpx2.AsyncClient],
) -> CreateClient:
    def factory(**kwargs: object) -> httpx2.AsyncClient:
        client = real_httpx2_factory(**kwargs)  # type: ignore[arg-type]
        created.append(client)
        return client

    return factory


def _assert_httpx2_model(build: Callable[[], object], patch_target: str) -> None:
    created: list[httpx2.AsyncClient] = []
    try:
        with patch(patch_target, side_effect=_recording_factory(created)):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                model = build()
        assert model is not None
        deprecations = [
            w for w in caught if issubclass(w.category, PydanticAIDeprecationWarning)
        ]
        assert deprecations == []
        assert len(created) == 1
        assert isinstance(created[0], httpx2.AsyncClient)
        assert not isinstance(created[0], httpx.AsyncClient)
    finally:
        for client in created:
            asyncio.run(client.aclose())


def test_meta_model_uses_httpx2_client() -> None:
    credentials = {"api_key": "sk-test", "api_base_url": "https://api.meta.ai/v1"}
    with patch(
        "code_puppy_core_plugins.meta_oauth.register_callbacks.load_credentials",
        return_value=credentials,
    ):
        _assert_httpx2_model(
            lambda: _create_meta_oauth_model(
                "meta-muse", {"type": "meta_oauth", "name": "muse-spark"}, {}
            ),
            "code_puppy.httpx2_utils.create_async_client",
        )


def test_grok_model_uses_httpx2_client() -> None:
    with patch(
        "code_puppy_core_plugins.grok_oauth.register_callbacks.get_valid_access_token",
        return_value="test-token",
    ):
        _assert_httpx2_model(
            lambda: _create_grok_oauth_model(
                "grok-4.5", {"type": "grok_oauth", "name": "grok-4.5"}, {}
            ),
            "code_puppy.httpx2_utils.create_async_client",
        )


def test_ollama_model_uses_httpx2_client(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    _assert_httpx2_model(
        lambda: create_ollama_model(
            "ollama-qwen3", {"type": "ollama", "name": "qwen3:4b"}, {}
        ),
        "code_puppy_core_plugins.ollama.register_callbacks.create_async_client",
    )
