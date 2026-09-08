"""Serialized credential rotation and atomic, private token persistence.

The sidecar lock covers read/exchange/write across threads and processes.
Interactive authentication writes use the same lock, so an older in-flight
refresh cannot overwrite a completed login. Network I/O runs only in callers'
worker threads when used from async code.
"""

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from threading import RLock

from code_puppy.atomic_io import path_lock

from .config import CLAUDE_CODE_OAUTH_CONFIG

logger = logging.getLogger(__name__)
_thread_lock = RLock()


@contextmanager
def token_transaction():
    from . import utils

    with _thread_lock, path_lock(str(utils.get_token_storage_path()), timeout=65):
        yield


def _save_tokens_unlocked(tokens):
    from . import utils

    path = utils.get_token_storage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".claude-token-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(tokens, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        return True
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_tokens(tokens):
    try:
        with token_transaction():
            return _save_tokens_unlocked(tokens)
    except Exception as exc:
        logger.error("Failed to save Claude tokens: %s", type(exc).__name__)
        return False


def remove_tokens():
    from . import utils

    with token_transaction():
        utils.get_token_storage_path().unlink(missing_ok=True)


def refresh_access_token(force=False, *, rejected_token=None):
    from . import utils

    observed = utils.load_stored_tokens() or {}
    rejected = (
        rejected_token if rejected_token is not None else observed.get("access_token")
    )
    try:
        with token_transaction():
            tokens = utils.load_stored_tokens()
            if not tokens:
                return None
            current = tokens.get("access_token")
            if not utils.is_token_expired(tokens) and (
                not force or current != rejected
            ):
                return current
            return _exchange_access_token(tokens)
    except Exception as exc:
        logger.error("Claude token refresh failed: %s", type(exc).__name__)
        return None


def _exchange_access_token(tokens) -> str | None:
    from . import utils

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        logger.debug("No refresh_token available")
        return None

    payload = {
        "grant_type": "refresh_token",
        "client_id": CLAUDE_CODE_OAUTH_CONFIG["client_id"],
        "refresh_token": refresh_token,
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
    }

    try:
        response = utils.requests.post(
            CLAUDE_CODE_OAUTH_CONFIG["token_url"],
            json=payload,
            headers=headers,
            timeout=30,
        )
        if response.status_code == 200:
            content_type = response.headers.get("content-type", "")
            if not content_type.startswith("application/json"):
                logger.error(
                    "Token refresh returned non-JSON response (Content-Type: %s): %s",
                    content_type,
                    "[body omitted]",
                )
                return None
            try:
                new_tokens = response.json()
            except (ValueError, json.JSONDecodeError) as e:
                logger.error("Failed to parse token refresh response as JSON: %s", e)
                return None
            if (
                not isinstance(new_tokens, dict)
                or not isinstance(new_tokens.get("access_token"), str)
                or not new_tokens["access_token"]
            ):
                logger.error("Token refresh returned no valid access token")
                return None
            tokens["access_token"] = new_tokens["access_token"]
            tokens["refresh_token"] = new_tokens.get("refresh_token", refresh_token)
            expires_in_value = new_tokens.get("expires_in")
            if expires_in_value is None:
                expires_in_value = tokens.get("expires_in")
            if expires_in_value is not None:
                tokens["expires_in"] = expires_in_value
                tokens["expires_at"] = utils._calculate_expires_at(expires_in_value)
            if _save_tokens_unlocked(tokens):
                utils.update_claude_code_model_tokens(tokens["access_token"])
                return tokens["access_token"]
        else:
            logger.error("Token refresh failed: HTTP %s", response.status_code)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.error("Token refresh error: %s", exc)
    return None
