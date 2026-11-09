"""Every direct Anthropic request must carry the claude-cli User-Agent.

Without it the OAuth token endpoint answers a misleading 429 "Rate limited",
which silently broke login (callback *and* paste-back) and token refresh.
"""

from unittest.mock import Mock, patch

import pytest

from code_puppy_core_plugins.claude_code_oauth import token_store
from code_puppy_core_plugins.claude_code_oauth.config import CLAUDE_CODE_OAUTH_CONFIG
from code_puppy_core_plugins.claude_code_oauth.utils import (
    _build_model_entry,
    assign_redirect_uri,
    exchange_code_for_tokens,
    fetch_claude_code_models,
    prepare_oauth_context,
)

UTILS = "code_puppy_core_plugins.claude_code_oauth.utils"
EXPECTED_UA = CLAUDE_CODE_OAUTH_CONFIG["user_agent"]


def _exchange_code():
    context = prepare_oauth_context()
    assign_redirect_uri(context, 8765)
    exchange_code_for_tokens("code", context)


def _refresh(monkeypatch):
    # A failed exchange arms a module-level backoff; keep it test-local.
    monkeypatch.setattr(token_store, "_exchange_blocked_until", 0.0)
    token_store._exchange_access_token({"refresh_token": "refresh"})


@pytest.mark.parametrize(
    ("method", "call"),
    [
        ("post", lambda mp: _exchange_code()),
        ("post", _refresh),
        ("get", lambda mp: fetch_claude_code_models("token")),
    ],
    ids=["code_exchange", "token_refresh", "models_fetch"],
)
def test_requests_send_claude_cli_user_agent(monkeypatch, method, call):
    response = Mock(status_code=429, text="Rate limited", headers={})
    with patch(f"{UTILS}.requests.{method}", return_value=response) as sent:
        call(monkeypatch)

    sent.assert_called_once()
    assert sent.call_args.kwargs["headers"]["User-Agent"] == EXPECTED_UA


def test_model_entry_uses_configured_user_agent():
    headers = _build_model_entry("claude-opus-5", "token", 200000)["custom_endpoint"][
        "headers"
    ]
    assert headers["User-Agent"] == EXPECTED_UA
    assert headers["anthropic-beta"].startswith(CLAUDE_CODE_OAUTH_CONFIG["oauth_beta"])
