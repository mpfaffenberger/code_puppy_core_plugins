"""Provider registry for the setup CLI.

One row per way Code Puppy can reach a model: OAuth core plugins and
plain API-key providers. Status checks import each plugin's own token
loader and only ever report *presence*, never credential values.

``sandbox_transport`` records how the Docker sbx kits can feed the
credential to a sandbox (see the sbx wizard):

- ``proxy-managed-env`` — static key read from an env var; sbx injects
  it at the proxy and the guest only sees a sentinel (proven for Meta).
- ``auth-in-sandbox`` — device/pasteback flow works headless in the
  guest; the token then lives in the guest.
- ``token-file-sync`` — host-authenticated token file would need syncing
  into the guest (experimental; host owns refresh).
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class OAuthProvider:
    id: str
    display: str
    plugin: str  # code_puppy_core_plugins submodule
    status_loader: str  # "module:attr" inside the plugin package
    auth_command: str  # slash command (without the slash)
    logout_command: str
    api_domains: tuple[str, ...]
    auth_domains: tuple[str, ...] = ()
    sandbox_transport: str = "token-file-sync"
    env_var: Optional[str] = None
    model_prefix: str = ""
    # "slash": login delegates to the plugin's interactive auth command.
    # "api-key": no auth command exists; setup collects a key directly.
    login_kind: str = "slash"


OAUTH_PROVIDERS: tuple[OAuthProvider, ...] = (
    OAuthProvider(
        id="meta",
        display="Meta Muse (OAuth)",
        plugin="meta_oauth",
        status_loader="utils:load_credentials",
        auth_command="meta-auth",
        logout_command="meta-logout",
        api_domains=("api.meta.ai",),
        auth_domains=("auth.meta.com",),
        sandbox_transport="proxy-managed-env",
        env_var="META_API_KEY",
        model_prefix="meta-",
    ),
    OAuthProvider(
        id="claude-code",
        display="Claude Code (OAuth)",
        plugin="claude_code_oauth",
        status_loader="utils:load_stored_tokens",
        auth_command="claude-code-auth",
        logout_command="claude-code-logout",
        api_domains=("api.anthropic.com",),
        auth_domains=("platform.claude.com", "claude.ai", "console.anthropic.com"),
        sandbox_transport="token-file-sync",
        model_prefix="claude-code-",
    ),
    OAuthProvider(
        id="codex",
        display="ChatGPT / Codex (OAuth)",
        plugin="chatgpt_oauth",
        status_loader="utils:load_stored_tokens",
        auth_command="chatgpt-auth",
        logout_command="chatgpt-logout",
        api_domains=("chatgpt.com",),
        auth_domains=("auth.openai.com",),
        sandbox_transport="token-file-sync",
        model_prefix="codex-",
    ),
    OAuthProvider(
        id="copilot",
        display="GitHub Copilot (device flow)",
        plugin="copilot_auth",
        status_loader="utils:load_device_tokens",
        auth_command="copilot-login",
        logout_command="copilot-logout",
        api_domains=("api.githubcopilot.com", "api.github.com"),
        auth_domains=("github.com",),
        sandbox_transport="auth-in-sandbox",
    ),
    OAuthProvider(
        id="grok",
        display="Grok / x.ai (OAuth)",
        plugin="grok_oauth",
        status_loader="utils:load_stored_tokens",
        auth_command="grok-auth",
        logout_command="grok-logout",
        api_domains=("api.x.ai",),
        auth_domains=("auth.x.ai", "accounts.x.ai"),
        sandbox_transport="token-file-sync",
    ),
    OAuthProvider(
        id="openrouter-oauth",
        display="OpenRouter (OAuth key mint)",
        plugin="openrouter_oauth",
        status_loader="",  # no token file; yields a plain API key
        # No slash command: the plugin hooks /add_model's credential step.
        auth_command="",
        logout_command="",
        login_kind="api-key",
        api_domains=("openrouter.ai",),
        auth_domains=("openrouter.ai",),
        sandbox_transport="proxy-managed-env",
        env_var="OPENROUTER_API_KEY",
    ),
)

_OAUTH_BY_ID = {provider.id: provider for provider in OAUTH_PROVIDERS}


def get_oauth_provider(provider_id: str) -> Optional[OAuthProvider]:
    return _OAUTH_BY_ID.get(provider_id.strip().lower())


def api_key_env_vars() -> tuple[str, ...]:
    """Credential env vars worth listing: core's own composite derivation.

    ``provider_credentials.credential_env_var_names()`` already unions
    the well-known provider keys with every ``$ENV`` referenced by a
    configured model, so a provider added via models.dev (whose
    ProviderInfo.env[0] was encoded into the model config at add time)
    appears here with no list to maintain in this plugin.
    """
    try:
        from code_puppy.provider_credentials import credential_env_var_names

        return tuple(sorted(credential_env_var_names()))
    except Exception:
        return ()


def _models_dev_env_vars() -> frozenset:
    """Every env var any models.dev provider declares (best effort).

    This is the open-ended source: a provider added to models.dev
    (e.g. ZAI's ZHIPU_API_KEY) works with ``setup auth`` without any
    code change here.
    """
    try:
        from code_puppy.models_dev_parser import ModelsDevRegistry

        providers_map = ModelsDevRegistry().get_providers()
        values = (
            providers_map.values() if isinstance(providers_map, dict) else providers_map
        )
        return frozenset(env for p in values for env in (p.env or []))
    except Exception:
        return frozenset()


def _models_dev_providers() -> dict[str, list[str]]:
    """models.dev provider id -> declared API-key env vars (best effort)."""
    try:
        from code_puppy.models_dev_parser import ModelsDevRegistry

        providers_map = ModelsDevRegistry().get_providers()
        values = (
            providers_map.values() if isinstance(providers_map, dict) else providers_map
        )
        return {p.id: list(p.env or []) for p in values if getattr(p, "id", None)}
    except Exception:
        return {}


def api_key_envs_for_provider(provider_id: str) -> Optional[list[str]]:
    """Env vars a models.dev provider declares; None if the id is unknown.

    ``env[0]`` is canonical: it is the name ``setup model add``
    encodes into generated model configs, so a login by provider id
    stores the key exactly where those models will look for it.
    """
    envs = _models_dev_providers().get(provider_id.strip().lower())
    return list(envs) if envs is not None else None


def is_known_api_key_env(env_var: str) -> bool:
    """Known via the curated lists OR declared by a models.dev provider."""
    return env_var in api_key_env_vars() or env_var in _models_dev_env_vars()


def credential_present(env_var: str) -> bool:
    """Whether a credential value exists (env or stored); never returns it."""
    if os.environ.get(env_var):
        return True
    try:
        from code_puppy.provider_credentials import get_credential_value

        return bool(get_credential_value(env_var))
    except Exception:
        pass
    try:
        from code_puppy.config import get_api_key

        return bool(get_api_key(env_var))
    except Exception:
        return False


def _load_status_callable(provider: OAuthProvider) -> Optional[Callable[[], Any]]:
    if not provider.status_loader:
        return None
    module_name, _, attr = provider.status_loader.partition(":")
    module = importlib.import_module(
        f"code_puppy_core_plugins.{provider.plugin}.{module_name}"
    )
    return getattr(module, attr, None)


def oauth_status(provider: OAuthProvider) -> dict[str, Any]:
    """Auth status for one OAuth provider; values are never included."""
    base = {
        "id": provider.id,
        "display": provider.display,
        "kind": "oauth",
        "plugin": provider.plugin,
        "sandbox_transport": provider.sandbox_transport,
        "api_domains": list(provider.api_domains),
    }
    if provider.id == "openrouter-oauth":
        present = credential_present(provider.env_var or "")
        return {
            **base,
            "authenticated": present,
            "source": "api-key" if present else "none",
        }
    try:
        loader = _load_status_callable(provider)
        loaded = loader() if loader else None
    except Exception as exc:  # plugin missing/broken must not break setup
        return {**base, "authenticated": False, "source": f"error: {exc}"}
    authenticated = _looks_authenticated(loaded)
    source = "none"
    if isinstance(loaded, dict):
        source = str(loaded.get("_source") or "token-file")
    elif loaded:
        source = "token-file"
    return {**base, "authenticated": authenticated, "source": source}


def _looks_authenticated(loaded: Any) -> bool:
    if not loaded:
        return False
    if isinstance(loaded, dict):
        return bool(loaded.get("api_key") or loaded.get("access_token"))
    if isinstance(loaded, (list, tuple)):
        return len(loaded) > 0
    return bool(
        getattr(loaded, "access_token", None) or getattr(loaded, "oauth_token", None)
    )


@dataclass
class ProviderStatus:
    oauth: list[dict[str, Any]] = field(default_factory=list)
    api_keys: list[dict[str, Any]] = field(default_factory=list)


def all_provider_status() -> ProviderStatus:
    status = ProviderStatus()
    status.oauth = [oauth_status(p) for p in OAUTH_PROVIDERS]
    status.api_keys = [
        {
            "env_var": env_var,
            "kind": "api-key",
            "authenticated": credential_present(env_var),
            "sandbox_transport": "proxy-managed-env",
        }
        for env_var in api_key_env_vars()
    ]
    return status
