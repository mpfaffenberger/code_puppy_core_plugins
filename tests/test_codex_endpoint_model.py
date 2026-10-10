"""Tests for the codex_endpoint model-type plugin."""

import asyncio

from code_puppy.chatgpt_codex_client import ChatGPTCodexAsyncClient
from code_puppy_core_plugins.codex_endpoint import register_callbacks as plugin


def _config(**endpoint):
    return {
        "type": "codex",
        "name": "gpt-5",
        "custom_endpoint": {"url": "https://codex.example.com/v1", **endpoint},
    }


def test_registers_the_codex_model_type():
    [entry] = plugin._get_codex_endpoint_model_types()

    assert entry["type"] == "codex"
    assert entry["handler"] is plugin.create_codex_endpoint_model


def test_builds_a_responses_model_over_the_codex_client():
    model = plugin.create_codex_endpoint_model(
        "gateway-gpt-5", _config(headers={"X-Api-Key": "k"}), {}
    )

    assert model is not None
    assert type(model).__name__ == "OpenAIResponsesModel"
    assert model.model_name == "gpt-5"
    assert str(model._provider.base_url).rstrip("/") == "https://codex.example.com/v1"
    client = model._provider._client._client
    assert isinstance(client, ChatGPTCodexAsyncClient)
    assert client.headers["X-Api-Key"] == "k"
    asyncio.run(model._provider._client.close())


def test_missing_endpoint_returns_none():
    assert plugin.create_codex_endpoint_model("bad", {"type": "codex"}, {}) is None


def test_model_factory_dispatches_codex_entries_to_the_plugin(monkeypatch):
    from code_puppy import callbacks
    from code_puppy.model_factory import ModelFactory

    monkeypatch.setitem(
        callbacks._callbacks,
        "register_model_type",
        [plugin._get_codex_endpoint_model_types],
    )
    config = {"gateway-gpt-5": _config()}

    model = ModelFactory.get_model("gateway-gpt-5", config)

    assert type(model).__name__ == "OpenAIResponsesModel"
    assert model.model_name == "gpt-5"
