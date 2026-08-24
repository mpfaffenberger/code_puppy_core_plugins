"""Configuration for the OpenRouter OAuth PKCE flow.

OpenRouter's flow is deliberately simple: no client id, no scopes, no
refresh tokens. The authorization code is exchanged at ``keys_url`` for a
user-controlled API key. Localhost callbacks are supported on any port, so
we bind an ephemeral one. See https://openrouter.ai/docs/use-cases/oauth-pkce
"""

from __future__ import annotations

OPENROUTER_OAUTH_CONFIG = {
    "auth_url": "https://openrouter.ai/auth",
    "keys_url": "https://openrouter.ai/api/v1/auth/keys",
    "env_var": "OPENROUTER_API_KEY",
    "provider_id": "openrouter",
    "redirect_host": "localhost",
    "redirect_path": "/callback",
    # OpenRouter authorization codes are single-use and expire after 10
    # minutes; give the user most of that window.
    "callback_timeout": 540,
}
