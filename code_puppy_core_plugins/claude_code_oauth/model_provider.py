"""Claude OAuth model construction; registration lives in register_callbacks."""

from typing import Any, Dict

from code_puppy.i18n import t
from code_puppy.messaging import emit_warning
from code_puppy.provider_identity import (
    make_anthropic_provider,
    resolve_provider_identity,
)

from .fast_mode import (
    FAST_SETTING_KEY,
    ensure_fast_beta_header,
    patch_anthropic_client_fast_mode,
)


def create_claude_code_model(model_name: str, model_config: Dict, config: Dict) -> Any:
    """Create a Claude Code model instance.

    This handler is registered via the 'register_model_type' callback to handle
    models with type='claude_code'.
    """
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel

    from code_puppy.claude_cache_client import ClaudeCacheAsyncClient
    from code_puppy.http_utils import get_cert_bundle_path
    from code_puppy.model_factory import (
        CONTEXT_1M_BETA,
        get_custom_config,
        make_model_settings,
    )

    from . import register_callbacks as callbacks

    get_valid_access_token = callbacks.get_valid_access_token
    _reauthenticate_after_expired_oauth = callbacks._reauthenticate_after_expired_oauth
    url, headers, verify, api_key, timeout = get_custom_config(model_config)

    # Refresh token if this is from the plugin
    if model_config.get("oauth_source") == "claude-code-plugin":
        refreshed_token = get_valid_access_token()
        api_key = refreshed_token
        if refreshed_token:
            custom_endpoint = model_config.get("custom_endpoint")
            if isinstance(custom_endpoint, dict):
                custom_endpoint["api_key"] = refreshed_token

    if not api_key:
        emit_warning(
            t(
                "oauth.claude.model.no_api_key",
                model=model_config.get("name") or "(unknown)",
            )
        )
        return None

    # Interleaved thinking (defaults True for OAuth models). NOTE: read via
    # get_all_model_settings — these plugin-owned settings aren't in core's
    # supported_settings allowlist (see fast_mode.FAST_SETTING_KEY).
    from code_puppy.config import get_all_model_settings

    per_model_settings = get_all_model_settings(model_name)
    interleaved_thinking = per_model_settings.get("interleaved_thinking", True)
    fast_enabled = bool(per_model_settings.get(FAST_SETTING_KEY, False))

    # Handle anthropic-beta header based on interleaved_thinking setting
    if "anthropic-beta" in headers:
        beta_parts = [p.strip() for p in headers["anthropic-beta"].split(",")]
        if interleaved_thinking:
            if "interleaved-thinking-2025-05-14" not in beta_parts:
                beta_parts.append("interleaved-thinking-2025-05-14")
        else:
            beta_parts = [p for p in beta_parts if "interleaved-thinking" not in p]
        headers["anthropic-beta"] = ",".join(beta_parts) if beta_parts else None
        if headers.get("anthropic-beta") is None:
            del headers["anthropic-beta"]
    elif interleaved_thinking:
        headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"

    # Add 1M context beta header for long-context models
    if model_config.get("context_length", 0) >= 1_000_000:
        if "anthropic-beta" in headers:
            beta_parts = [p.strip() for p in headers["anthropic-beta"].split(",")]
            if CONTEXT_1M_BETA not in beta_parts:
                beta_parts.append(CONTEXT_1M_BETA)
            headers["anthropic-beta"] = ",".join(beta_parts)
        else:
            headers["anthropic-beta"] = CONTEXT_1M_BETA

    # Fast mode: append fast-mode-2026-02-01 beta marker when enabled
    ensure_fast_beta_header(headers, fast_enabled)

    # Use a dedicated client wrapper for OAuth refresh and tool-name transport
    # transformations; prompt-cache markers are owned by model settings.
    if verify is None:
        verify = get_cert_bundle_path()

    # No HTTP/2 for OAuth: the UnprefixingStream tool-name rewrite breaks under
    # HTTP/2's compression handling, causing zlib decompression errors.
    kwargs = {}
    # Plugin releases land before core. Old transports keep their legacy path.
    import inspect
    from .runtime_credentials import current_token, refresh_token

    if "oauth_token_provider" in inspect.signature(ClaudeCacheAsyncClient).parameters:
        kwargs.update(
            oauth_token_provider=current_token, oauth_refresh_callback=refresh_token
        )
    client = ClaudeCacheAsyncClient(
        **kwargs,
        headers=headers,
        verify=verify,
        timeout=180,
        http2=False,
        # Claude Code OAuth requires the ``cp_`` tool-name prefix; the wire
        # format Anthropic's CLI uses won't accept un-prefixed tools.
        apply_claude_code_prefix=True,
        oauth_reauthentication_callback=lambda: _reauthenticate_after_expired_oauth(
            model_name
        ),
    )

    anthropic_client = AsyncAnthropic(
        base_url=url,
        http_client=client,
        auth_token=api_key,
    )

    def _update_runtime_token(access_token: str) -> None:
        anthropic_client.auth_token = access_token
        custom_endpoint = model_config.get("custom_endpoint")
        if isinstance(custom_endpoint, dict):
            custom_endpoint["api_key"] = access_token

    client.set_token_update_callback(_update_runtime_token)
    # Fast mode wrapper re-reads the setting on every call so
    # /claude-code-fast takes effect live.

    patch_anthropic_client_fast_mode(anthropic_client, model_name)
    anthropic_client.api_key = None
    anthropic_client.auth_token = api_key
    provider = make_anthropic_provider(
        resolve_provider_identity(model_name, model_config),
        anthropic_client=anthropic_client,
    )
    # Prompt caching belongs to pydantic-ai's native Anthropic settings, not
    # the transport shim. OAuth subscription models receive the free one-hour
    # TTL at all three cache breakpoints.
    model_settings = make_model_settings(model_name)
    model_settings.update(
        {
            "anthropic_cache_instructions": "1h",
            "anthropic_cache_tool_definitions": "1h",
            "anthropic_cache_messages": "1h",
        }
    )
    return AnthropicModel(
        model_name=model_config["name"],
        provider=provider,
        settings=model_settings,
    )
