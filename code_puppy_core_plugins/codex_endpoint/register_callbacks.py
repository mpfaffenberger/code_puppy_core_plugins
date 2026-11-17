"""Model type for custom endpoints that speak the ChatGPT Codex Responses API.

``chatgpt_oauth`` talks to ChatGPT's own Codex backend with OAuth. Gateways and
self-hosted proxies that front the same backend need the same wire format --
``store: false``, mandatory streaming, no ``max_output_tokens`` -- but with
their own URL and credentials. ``custom_openai_responses`` sends standard
Responses requests, without the request rules the Codex API requires (see
``code_puppy.chatgpt_codex_client``).

Registers the ``codex`` model type, configured like ``custom_openai`` in
``~/.code_puppy/extra_models.json``:
{
    "gateway-gpt-5": {
        "type": "codex",
        "name": "gpt-5",
        "custom_endpoint": {
            "url": "https://codex-gateway.example.com/v1",
            "headers": {"X-Api-Key": "$CODEX_GATEWAY_KEY"}
        }
    }
}

Requests go through core's ``ChatGPTCodexAsyncClient`` (the client
``chatgpt_oauth`` uses), which applies the Codex request rules.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from code_puppy.callbacks import register_callback

if TYPE_CHECKING:
    from pydantic_ai.models.openai import OpenAIResponsesModel

logger = logging.getLogger(__name__)


def create_codex_endpoint_model(
    model_name: str,
    model_config: dict[str, Any],
    config: dict[str, Any],
) -> OpenAIResponsesModel | None:
    """Build an ``OpenAIResponsesModel`` for a custom Codex endpoint.

    Args:
        model_name: The config key name of the model.
        model_config: The model's configuration dict; needs ``custom_endpoint``.
        config: The full models configuration (unused, kept for API compat).

    Returns:
        The model, or None if the entry is incomplete or creation fails.
    """
    # Imported here, not at module scope: this plugin loads on every boot and
    # only runs that pick a codex model should pay for the openai SDK.
    from pydantic_ai.models.openai import OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from code_puppy.chatgpt_codex_client import create_codex_async_client
    from code_puppy.model_factory import get_custom_config

    try:
        url, headers, verify, api_key, _timeout = get_custom_config(model_config)
        client = create_codex_async_client(headers=headers, verify=verify)
        provider_args: dict[str, Any] = {"base_url": url, "http_client": client}
        if api_key:
            provider_args["api_key"] = api_key
        provider = OpenAIProvider(**provider_args)
        return OpenAIResponsesModel(
            model_config.get("name", model_name), provider=provider
        )
    except Exception as e:
        logger.error("Failed to create codex model '%s': %s", model_name, e)
        return None


def _get_codex_endpoint_model_types() -> list[dict[str, Any]]:
    """Return the codex model type handler for the register_model_type hook."""
    return [{"type": "codex", "handler": create_codex_endpoint_model}]


register_callback("register_model_type", _get_codex_endpoint_model_types)
