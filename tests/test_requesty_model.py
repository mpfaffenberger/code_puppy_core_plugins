"""Tests for the Requesty model plugin."""

import asyncio
from unittest.mock import patch

from code_puppy_core_plugins.requesty.register_callbacks import (
    _get_requesty_model_types,
    create_requesty_model,
)


def _no_config(key):
    return None


@patch("code_puppy.shared_credentials.get", side_effect=_no_config)
@patch("code_puppy.model_factory.get_value", side_effect=_no_config)
def test_default_env_key_uses_requesty_endpoint(_value, _get, monkeypatch):
    """Without api_key in the config, REQUESTY_API_KEY is used."""
    monkeypatch.setenv("REQUESTY_API_KEY", "test-key")

    model = create_requesty_model(
        "requesty-gpt-4o-mini",
        {"type": "requesty", "name": "openai/gpt-4o-mini"},
        {},
    )

    assert model is not None
    assert model.model_name == "openai/gpt-4o-mini"
    assert model._provider.name == "requesty"
    assert str(model._provider.base_url).rstrip("/") == "https://router.requesty.ai/v1"
    assert model.profile.get("openai_chat_supports_multiple_system_messages") is False
    asyncio.run(model._provider._client.close())


def test_raw_api_key_in_config():
    """A raw api_key value in the model config is used as is."""
    model = create_requesty_model(
        "requesty-gpt-4o-mini",
        {"type": "requesty", "name": "openai/gpt-4o-mini", "api_key": "raw-key"},
        {},
    )

    assert model is not None
    assert model._provider.client.api_key == "raw-key"
    asyncio.run(model._provider._client.close())


@patch("code_puppy.shared_credentials.get", side_effect=_no_config)
@patch("code_puppy.model_factory.get_value", side_effect=_no_config)
def test_env_var_reference_in_config(_value, _get, monkeypatch):
    """An api_key of the form $VAR is resolved from the environment."""
    monkeypatch.setenv("ROUTER_API_KEY", "env-key")

    model = create_requesty_model(
        "requesty-gpt-4o-mini",
        {
            "type": "requesty",
            "name": "openai/gpt-4o-mini",
            "api_key": "$ROUTER_API_KEY",
        },
        {},
    )

    assert model is not None
    assert model._provider.client.api_key == "env-key"
    asyncio.run(model._provider._client.close())


@patch("code_puppy.shared_credentials.get", side_effect=_no_config)
@patch("code_puppy.model_factory.get_value", side_effect=_no_config)
def test_missing_key_skips_model(_value, _get, monkeypatch):
    """No key anywhere returns None instead of raising."""
    monkeypatch.delenv("REQUESTY_API_KEY", raising=False)

    model = create_requesty_model(
        "requesty-gpt-4o-mini",
        {"type": "requesty", "name": "openai/gpt-4o-mini"},
        {},
    )

    assert model is None


def test_registers_requesty_model_type():
    """The plugin advertises the requesty type to the model factory."""
    types = _get_requesty_model_types()

    assert [entry["type"] for entry in types] == ["requesty"]
    assert types[0]["handler"] is create_requesty_model
