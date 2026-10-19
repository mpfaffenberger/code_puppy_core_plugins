"""Requesty model type handler for the Requesty LLM router.

Registers the 'requesty' model type so users can reach any model served by
Requesty (https://requesty.ai) through its OpenAI-compatible API, configured
in ~/.code_puppy/extra_models.json.

Minimal config (key from REQUESTY_API_KEY, set with /set or the environment):
{
    "requesty-gpt-4o-mini": {
        "type": "requesty",
        "name": "openai/gpt-4o-mini",
        "context_length": 128000
    }
}

The key can also be given per model, as a raw value or an env var reference:
{
    "requesty-claude-sonnet": {
        "type": "requesty",
        "name": "anthropic/claude-sonnet-4-5",
        "context_length": 200000,
        "api_key": "$REQUESTY_API_KEY"
    }
}

Get a key at https://app.requesty.ai/api-keys, docs at https://docs.requesty.ai.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from code_puppy.callbacks import register_callback
from code_puppy.messaging import emit_warning

if TYPE_CHECKING:
    from pydantic_ai.models.openai import OpenAIChatModel

_REQUESTY_BASE_URL = "https://router.requesty.ai/v1"
_REQUESTY_API_KEY_ENV = "REQUESTY_API_KEY"


def _resolve_api_key(model_config: dict[str, Any]) -> str | None:
    """Return the Requesty key from the model config or REQUESTY_API_KEY."""
    from code_puppy.model_factory import get_api_key

    api_key_config = model_config.get("api_key")
    if api_key_config:
        if api_key_config.startswith("$"):
            env_var_name = api_key_config[1:]
            api_key = get_api_key(env_var_name)
            if api_key is None:
                emit_warning(
                    f"Requesty API key '{env_var_name}' not found (check config "
                    f"or environment); skipping model '{model_config.get('name')}'."
                )
            return api_key
        return api_key_config

    api_key = get_api_key(_REQUESTY_API_KEY_ENV)
    if api_key is None:
        emit_warning(
            f"{_REQUESTY_API_KEY_ENV} is not set (check config or environment); "
            f"skipping Requesty model '{model_config.get('name')}'."
        )
    return api_key


def create_requesty_model(
    model_name: str,
    model_config: dict[str, Any],
    config: dict[str, Any],
) -> OpenAIChatModel | None:
    """Create an OpenAI Chat Completions model routed through Requesty.

    Args:
        model_name: The config key name of the model.
        model_config: The model's configuration dict.
        config: The full models configuration (unused, kept for API compat).

    Returns:
        OpenAIChatModel instance, or None when no API key is available.
    """
    del config
    api_key = _resolve_api_key(model_config)
    if not api_key:
        return None

    # Imported here, not at module scope: this plugin loads on every boot and
    # the openai SDK behind OpenAIChatModel is slow to import cold.
    from pydantic_ai.models.openai import OpenAIChatModel

    from code_puppy.httpx2_utils import create_async_client
    from code_puppy.model_factory import _strict_openai_profile
    from code_puppy.provider_identity import make_openai_provider

    provider = make_openai_provider(
        "requesty",
        api_key=api_key,
        base_url=_REQUESTY_BASE_URL,
        http_client=create_async_client(model_name=model_name),
    )
    return OpenAIChatModel(
        model_name=model_config["name"],
        provider=provider,
        profile=_strict_openai_profile(model_name, model_config),
    )


def _get_requesty_model_types():
    """Return the requesty model type handler for the register_model_type hook."""
    return [
        {
            "type": "requesty",
            "handler": create_requesty_model,
        },
    ]


register_callback("register_model_type", _get_requesty_model_types)
