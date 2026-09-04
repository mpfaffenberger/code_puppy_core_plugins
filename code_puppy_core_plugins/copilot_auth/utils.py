"""Utility helpers for the GitHub Copilot auth plugin.

Handles browser-based Device Flow authentication, session-token exchange /
caching, and model registration persistence.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from .config import (
    COPILOT_AUTH_CONFIG,
    COPILOT_MODEL_CONTEXT_LENGTHS,
    DEFAULT_COPILOT_MODELS,
    DEVICE_FLOW_CONFIG,
    get_copilot_models_path,
    get_device_token_storage_path,
    get_session_cache_path,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token storage — persisted tokens obtained via the Device Flow
# ---------------------------------------------------------------------------


@dataclass
class CopilotToken:
    """An OAuth token for a GitHub host."""

    host: str  # "github.com" or a GHE hostname
    oauth_token: str
    user: str = ""


def get_token_for_host(host: str) -> Optional[CopilotToken]:
    """Return a stored Device Flow token whose host matches *host* exactly.

    Returns ``None`` if no token for the given host is found.
    """
    tokens = load_device_tokens()
    for t in tokens:
        if t.host == host:
            return t
    return None


# ---------------------------------------------------------------------------
# Device Flow — browser-based GitHub OAuth (no IDE required)
# ---------------------------------------------------------------------------


def start_device_flow(host: str = "github.com") -> Optional[Dict[str, Any]]:
    """Initiate the GitHub Device Flow and return the device code response.

    Returns a dict with ``device_code``, ``user_code``, ``verification_uri``,
    ``expires_in``, and ``interval`` on success, or ``None`` on failure.
    """
    url = DEVICE_FLOW_CONFIG["device_code_url"].format(host=host)
    payload = {
        "client_id": DEVICE_FLOW_CONFIG["client_id"],
        "scope": DEVICE_FLOW_CONFIG["scope"],
    }
    headers = {"Accept": "application/json"}
    try:
        resp = requests.post(url, data=payload, headers=headers, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("device_code") and data.get("user_code"):
                return data
            logger.warning("Device flow response missing required fields: %s", data)
        else:
            logger.warning(
                "Device flow initiation failed: %s %s",
                resp.status_code,
                resp.text[:300],
            )
    except Exception as exc:
        logger.warning("Device flow error: %s", exc)
    return None


def poll_for_token(
    device_code: str,
    host: str = "github.com",
    interval: int = 5,
    expires_in: int = 900,
) -> Optional[str]:
    """Poll GitHub until the user completes the Device Flow authorization.

    Returns the OAuth ``access_token`` on success, or ``None`` on timeout/denial.
    """
    url = DEVICE_FLOW_CONFIG["access_token_url"].format(host=host)
    payload = {
        "client_id": DEVICE_FLOW_CONFIG["client_id"],
        "device_code": device_code,
        "grant_type": DEVICE_FLOW_CONFIG["grant_type"],
    }
    headers = {"Accept": "application/json"}

    deadline = time.time() + expires_in
    poll_interval = max(interval, DEVICE_FLOW_CONFIG["default_poll_interval"])

    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            resp = requests.post(url, data=payload, headers=headers, timeout=30)
            data = resp.json() if resp.status_code == 200 else {}
        except Exception as exc:
            logger.warning("Device flow poll error: %s", exc)
            continue

        token = data.get("access_token")
        if token:
            return token

        error = data.get("error", "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            poll_interval += 5
            continue
        if error in ("expired_token", "access_denied", "unsupported_grant_type"):
            logger.warning("Device flow denied or expired: %s", error)
            return None
        # Unknown error — keep trying until deadline
        logger.debug("Device flow poll returned: %s", data)

    return None


def save_device_token(host: str, oauth_token: str, user: str = "") -> bool:
    """Persist a token obtained via the Device Flow to disk."""
    try:
        path = get_device_token_storage_path()
        data: Dict[str, Any] = {}
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        data[host] = {
            "oauth_token": oauth_token,
            "user": user,
            "created_at": time.time(),
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        path.chmod(0o600)
        return True
    except Exception as exc:
        logger.error("Failed to save device token: %s", exc)
        return False


def load_device_tokens() -> List[CopilotToken]:
    """Load tokens previously obtained via the Device Flow."""
    tokens: List[CopilotToken] = []
    try:
        path = get_device_token_storage_path()
        if not path.exists():
            return tokens
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for host, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                oauth_token = entry.get("oauth_token")
                if oauth_token:
                    tokens.append(
                        CopilotToken(
                            host=host,
                            oauth_token=oauth_token,
                            user=entry.get("user", ""),
                        )
                    )
    except Exception as exc:
        logger.warning("Failed to load device tokens: %s", exc)
    return tokens


# ---------------------------------------------------------------------------
# Session token exchange — converts long-lived OAuth token → short-lived
# Copilot API bearer token (typically valid ~30 min).
# ---------------------------------------------------------------------------


@dataclass
class SessionToken:
    """A short-lived Copilot API session token."""

    token: str
    expires_at: float  # Unix timestamp
    api_endpoint: str = (
        ""  # API base URL from the token response (may differ per region/host)
    )


# Module-level cache keyed by (host, oauth_token[:16])
_session_cache: Dict[str, SessionToken] = {}

# Stores the API base URL returned by the most recent session-token exchange
# per host, so that model registration can use it.
_host_api_endpoints: Dict[str, str] = {}


def _cache_key(oauth_token: str, host: str) -> str:
    return f"{host}:{oauth_token[:16]}"


def _token_endpoint(host: str) -> str:
    """Return the Copilot session-token endpoint for the given host."""
    if host == "github.com":
        return COPILOT_AUTH_CONFIG["github_token_url"]
    return COPILOT_AUTH_CONFIG["ghe_token_url_template"].format(host=host)


def exchange_for_session_token(
    oauth_token: str, host: str = "github.com"
) -> Optional[SessionToken]:
    """Exchange a GitHub OAuth token for a short-lived Copilot session token.

    The response ``endpoints.api`` value is the correct API base URL for this
    token and may differ from the default (e.g. regional or GHE deployments).
    """
    url = _token_endpoint(host)
    headers = {
        "Authorization": f"token {oauth_token}",
        "Accept": "application/json",
        "Editor-Version": COPILOT_AUTH_CONFIG["editor_version"],
        "Editor-Plugin-Version": COPILOT_AUTH_CONFIG["editor_plugin_version"],
        "Copilot-Integration-Id": COPILOT_AUTH_CONFIG["copilot_integration_id"],
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            token_str = data.get("token")
            expires_at = data.get("expires_at", 0)
            if token_str:
                # Extract the API endpoint — the token is only valid against
                # this specific URL. Falls back to the default if missing.
                endpoints = data.get("endpoints") or {}
                api_endpoint = (
                    endpoints.get("api", "").rstrip("/")
                    or COPILOT_AUTH_CONFIG["api_base_url"]
                )
                # Remember this for model registration
                _host_api_endpoints[host] = api_endpoint
                logger.debug(
                    "Copilot session for %s: api_endpoint=%s", host, api_endpoint
                )

                st = SessionToken(
                    token=token_str,
                    expires_at=float(expires_at),
                    api_endpoint=api_endpoint,
                )
                # Persist to disk so we survive process restarts within the window
                _persist_session(st, host, oauth_token)
                return st
            logger.warning("Token endpoint returned 200 but no 'token' field")
        elif resp.status_code == 401:
            logger.warning(
                "Copilot token exchange returned 401 — OAuth token may be revoked."
            )
        else:
            logger.warning(
                "Copilot token exchange failed: %s %s",
                resp.status_code,
                resp.text[:200],
            )
    except requests.exceptions.Timeout:
        logger.warning("Timeout exchanging Copilot token for host %s", host)
    except Exception as exc:
        logger.warning("Copilot token exchange error: %s", exc)
    return None


def _persist_session(st: SessionToken, host: str, oauth_token: str = "") -> None:
    """Write session token to disk for reuse after restarts.

    Stores a fingerprint of the OAuth token so that a persisted session is
    only reused when the same OAuth token is still active.
    """
    try:
        path = get_session_cache_path()
        data: Dict[str, Any] = {}
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        data[host] = {
            "token": st.token,
            "expires_at": st.expires_at,
            "api_endpoint": st.api_endpoint,
            "oauth_fingerprint": oauth_token[:16] if oauth_token else "",
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        path.chmod(0o600)
    except Exception as exc:
        logger.debug("Could not persist session token: %s", exc)


def _load_persisted_session(host: str, oauth_token: str = "") -> Optional[SessionToken]:
    """Load a previously persisted session token from disk.

    If *oauth_token* is provided, the persisted entry is only returned when
    its stored fingerprint matches ``oauth_token[:16]``, preventing
    cross-account reuse after a re-login.
    """
    try:
        path = get_session_cache_path()
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        entry = data.get(host)
        if entry:
            # Verify the stored fingerprint matches the current OAuth token
            stored_fp = entry.get("oauth_fingerprint", "")
            if oauth_token and stored_fp and stored_fp != oauth_token[:16]:
                logger.debug(
                    "Persisted session fingerprint mismatch for %s — skipping", host
                )
                return None

            api_endpoint = entry.get(
                "api_endpoint", COPILOT_AUTH_CONFIG["api_base_url"]
            )
            # Restore the host→endpoint mapping
            if api_endpoint:
                _host_api_endpoints[host] = api_endpoint
            return SessionToken(
                token=entry["token"],
                expires_at=float(entry["expires_at"]),
                api_endpoint=api_endpoint,
            )
    except Exception as exc:
        logger.debug("Could not load persisted session: %s", exc)
    return None


def get_valid_session_token(
    oauth_token: str, host: str = "github.com"
) -> Optional[str]:
    """Return a valid Copilot session token, refreshing if needed.

    Checks in-memory cache → on-disk cache → exchanges for a new one.
    Returns the raw bearer token string or ``None``.
    """
    key = _cache_key(oauth_token, host)

    # 1) In-memory cache
    cached = _session_cache.get(key)
    if cached and cached.expires_at > time.time() + 60:
        # Ensure host→endpoint mapping is populated
        if cached.api_endpoint:
            _host_api_endpoints[host] = cached.api_endpoint
        return cached.token

    # 2) On-disk cache (with fingerprint verification)
    persisted = _load_persisted_session(host, oauth_token)
    if persisted and persisted.expires_at > time.time() + 60:
        _session_cache[key] = persisted
        return persisted.token

    # 3) Exchange
    new_token = exchange_for_session_token(oauth_token, host)
    if new_token:
        _session_cache[key] = new_token
        return new_token.token

    return None


def get_api_endpoint_for_host(host: str) -> str:
    """Return the Copilot API base URL for the given host.

    The correct endpoint is discovered during session-token exchange
    (the ``endpoints.api`` field in the response).  Falls back to the
    default ``api.githubcopilot.com`` if no exchange has happened yet.
    """
    return _host_api_endpoints.get(host, COPILOT_AUTH_CONFIG["api_base_url"])


def clear_caches() -> None:
    """Reset all in-memory session and endpoint caches.

    Called by ``/copilot-logout`` to ensure no stale Copilot state remains.
    """
    _session_cache.clear()
    _host_api_endpoints.clear()


# ---------------------------------------------------------------------------
# Model persistence — copilot_models.json
# ---------------------------------------------------------------------------


def load_copilot_models() -> Dict[str, Any]:
    """Load registered Copilot models from copilot_models.json."""
    try:
        path = get_copilot_models_path()
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
            logger.warning("copilot_models.json is not a JSON object — ignoring")
    except Exception as exc:
        logger.error("Failed to load Copilot models: %s", exc)
    return {}


def save_copilot_models(models: Dict[str, Any]) -> bool:
    """Persist Copilot models to copilot_models.json."""
    try:
        path = get_copilot_models_path()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(models, fh, indent=2)
        return True
    except Exception as exc:
        logger.error("Failed to save Copilot models: %s", exc)
    return False


def remove_copilot_models() -> int:
    """Remove all Copilot-sourced models from copilot_models.json."""
    try:
        models = load_copilot_models()
        to_remove = [
            name
            for name, cfg in models.items()
            if cfg.get("oauth_source") == "copilot-auth-plugin"
        ]
        for name in to_remove:
            models.pop(name, None)
        if save_copilot_models(models):
            return len(to_remove)
    except Exception as exc:
        logger.error("Error removing Copilot models: %s", exc)
    return 0


# API families a Copilot model can be driven through.  Stored on each
# registered model as ``copilot_api`` so ``_create_copilot_model`` knows
# whether to build a Chat Completions or a Responses model.
COPILOT_API_CHAT = "chat"
COPILOT_API_RESPONSES = "responses"

_CHAT_COMPLETIONS_ENDPOINT = "/chat/completions"
_RESPONSES_ENDPOINT = "/responses"


def preferred_api_for_endpoints(supported_endpoints: Any) -> str:
    """Pick the API family for a model from its ``supported_endpoints``.

    The Copilot ``/models`` catalogue lists, per model, which HTTP paths it
    accepts (e.g. ``["/responses", "ws:/responses"]`` for GPT-5.5/5.6, or
    ``["/chat/completions", "/v1/messages"]`` for Claude).  Chat Completions
    is the default; Responses is only chosen when the model does *not*
    accept Chat Completions, so dual-endpoint models keep today's behaviour.
    """
    if not isinstance(supported_endpoints, list):
        return COPILOT_API_CHAT
    endpoints = {str(ep).strip().lower() for ep in supported_endpoints}
    if _RESPONSES_ENDPOINT in endpoints and _CHAT_COMPLETIONS_ENDPOINT not in endpoints:
        return COPILOT_API_RESPONSES
    return COPILOT_API_CHAT


def _catalogue_entry(raw: Any) -> Optional[Dict[str, Any]]:
    """Normalise one ``/models`` item (dict or bare id) to a plugin entry.

    Returns ``{"id", "api", "context_length"}`` or ``None`` when the item has
    no usable id.  ``context_length`` is the catalogue's
    ``max_context_window_tokens`` when present, else ``None``.

    Already-normalised entries (the output of this function, as handed back
    to :func:`add_models_to_config`) pass through unchanged.
    """
    if isinstance(raw, str):
        if not raw:
            return None
        return {"id": raw, "api": COPILOT_API_CHAT, "context_length": None}
    if not isinstance(raw, dict):
        return None
    model_id = raw.get("id") or raw.get("name")
    if not model_id:
        return None

    api = raw.get("api")
    if api in (COPILOT_API_CHAT, COPILOT_API_RESPONSES):
        # Already normalised -- keep the api decision made from the catalogue.
        ctx = raw.get("context_length")
    else:
        api = preferred_api_for_endpoints(raw.get("supported_endpoints"))
        capabilities = raw.get("capabilities") or {}
        limits = capabilities.get("limits") if isinstance(capabilities, dict) else None
        ctx = (limits or {}).get("max_context_window_tokens")

    return {
        "id": model_id,
        "api": api,
        "context_length": ctx if isinstance(ctx, int) and ctx > 0 else None,
    }


def fetch_copilot_models(
    session_token: str, host: str = "github.com"
) -> List[Dict[str, Any]]:
    """Try to fetch the model catalogue from the Copilot API.

    Returns a list of ``{"id", "api", "context_length"}`` dicts (see
    :func:`_catalogue_entry`).  Falls back to ``DEFAULT_COPILOT_MODELS`` if
    the endpoint is unavailable.  Uses the host-specific API endpoint
    discovered during token exchange.
    """
    api_base = get_api_endpoint_for_host(host)
    url = f"{api_base}/models"
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Accept": "application/json",
        "Editor-Version": COPILOT_AUTH_CONFIG["editor_version"],
        "Editor-Plugin-Version": COPILOT_AUTH_CONFIG["editor_plugin_version"],
        "Copilot-Integration-Id": COPILOT_AUTH_CONFIG["copilot_integration_id"],
        "Openai-Intent": COPILOT_AUTH_CONFIG["openai_intent"],
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            model_list = data.get("data") or data.get("models") or []
            if isinstance(model_list, list):
                entries = [
                    entry
                    for entry in (_catalogue_entry(m) for m in model_list)
                    if entry is not None
                ]
                if entries:
                    logger.info("Fetched %d models from Copilot API", len(entries))
                    return entries
    except Exception as exc:
        logger.debug("Could not fetch Copilot model list: %s", exc)

    logger.info("Using default Copilot model list")
    return [
        entry
        for entry in (_catalogue_entry(m) for m in DEFAULT_COPILOT_MODELS)
        if entry is not None
    ]


def _is_claude_model(model_name: str) -> bool:
    """Check if a model name refers to a Claude/Anthropic model."""
    return model_name.lower().startswith("claude-")


def _is_openai_model(model_name: str) -> bool:
    """Check if a model name refers to an OpenAI/GPT model."""
    lower = model_name.lower()
    return lower.startswith("gpt-") or lower.startswith("o3") or lower.startswith("o4")


def _build_claude_model_settings(model_name: str) -> Dict[str, Any]:
    """Build model_settings fields for a Claude model.

    Mirrors the conventions in the claude_code_oauth plugin's
    ``_build_model_entry``.
    """
    supported_settings = [
        "temperature",
        "extended_thinking",
        "budget_tokens",
        "interleaved_thinking",
    ]

    # Opus 4-6 (e.g. claude-opus-4.6) supports the effort setting,
    # same as the claude_code_oauth plugin.
    lower = model_name.lower()
    if "opus-4.6" in lower or "opus-4-6" in lower or "4-6-opus" in lower:
        supported_settings.append("effort")

    return {"supported_settings": supported_settings}


def _build_openai_model_settings(model_name: str) -> Dict[str, Any]:
    """Build model_settings fields for an OpenAI/GPT model behind Copilot.

    The Copilot API only proxies a subset of OpenAI features.  Currently
    none of the GPT models exposed through Copilot support
    ``reasoning_effort``, ``summary``, or ``verbosity`` — sending those
    parameters results in a 400 Bad Request.  Only basic ``temperature``
    is safe.
    """
    return {"supported_settings": ["temperature"]}


def _model_settings_for(model_name: str) -> Dict[str, Any]:
    """Return family-specific config fields for a Copilot model.

    Claude models get extended_thinking/effort/etc.; OpenAI models get
    reasoning_effort/summary/verbosity; everything else gets temperature.
    """
    if _is_claude_model(model_name):
        return _build_claude_model_settings(model_name)
    if _is_openai_model(model_name):
        return _build_openai_model_settings(model_name)
    # Fallback for other providers (Gemini, etc.)
    return {"supported_settings": ["temperature"]}


def add_models_to_config(
    models: List[Any],
    host: str = "github.com",
) -> bool:
    """Register Copilot models in copilot_models.json.

    *models* is what :func:`fetch_copilot_models` returns; bare id strings
    are also accepted and treated as Chat Completions models.
    """
    try:
        copilot_models = load_copilot_models()
        added = 0
        prefix = COPILOT_AUTH_CONFIG["prefix"]
        # Use the API endpoint from session-token exchange (critical for GHE).
        api_url = get_api_endpoint_for_host(host)
        for raw in models:
            catalogue = _catalogue_entry(raw)
            if catalogue is None:
                continue
            model_name = catalogue["id"]
            prefixed = f"{prefix}{model_name}"
            context_length = catalogue[
                "context_length"
            ] or COPILOT_MODEL_CONTEXT_LENGTHS.get(
                model_name,
                COPILOT_AUTH_CONFIG["default_context_length"],
            )
            entry: Dict[str, Any] = {
                "type": "copilot",
                "name": model_name,
                "custom_endpoint": {
                    "url": api_url,
                },
                "context_length": context_length,
                "copilot_host": host,
                "oauth_source": "copilot-auth-plugin",
                # Which API family the model is served through; consumed by
                # ``_create_copilot_model`` to pick Chat vs Responses.
                "copilot_api": catalogue["api"],
            }
            # Merge family-specific settings (supported_settings, etc.)
            entry.update(_model_settings_for(model_name))
            copilot_models[prefixed] = entry
            added += 1
        if save_copilot_models(copilot_models):
            logger.info("Registered %d Copilot models (api: %s)", added, api_url)
            return True
    except Exception as exc:
        logger.error("Error adding Copilot models to config: %s", exc)
    return False
