from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from code_puppy_core_plugins.logfire_oauth import oauth
from code_puppy_core_plugins.logfire_oauth import register_callbacks as callbacks


@pytest.fixture
def credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "oauth.json"
    monkeypatch.setenv("CODE_PUPPY_LOGFIRE_CREDENTIALS", str(path))
    monkeypatch.setenv(
        "CODE_PUPPY_LOGFIRE_PROJECT_CREDENTIALS", str(tmp_path / "project.json")
    )
    return path


def test_generate_pkce_is_s256_compatible() -> None:
    verifier, challenge = oauth.generate_pkce()
    assert verifier
    assert challenge
    assert "=" not in challenge
    assert verifier != challenge


def test_token_storage_round_trip(credentials: Path) -> None:
    tokens = oauth.OAuthTokens(
        access_token="access-secret",
        refresh_token="refresh-secret",
        token_type="Bearer",
        expires_at=1234,
        scope="project:read",
        base_url="https://example.com",
        client_id="client",
    )
    oauth.save_tokens(tokens)

    assert oauth.load_tokens() == tokens
    assert credentials.stat().st_mode & 0o777 == 0o600
    assert oauth.delete_tokens() is True
    assert oauth.delete_tokens() is False


def test_dynamic_client_registration_uses_device_grant() -> None:
    request_seen: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_seen
        request_seen = request
        return httpx.Response(201, json={"client_id": "lf_dcr_puppy"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client_id = oauth.register_dynamic_client(
        client,
        {"registration_endpoint": "https://example.com/api/oauth/register"},
        oauth.ONBOARD_SCOPES,
    )

    assert client_id == "lf_dcr_puppy"
    assert request_seen is not None
    payload = json.loads(request_seen.content)
    assert payload["client_name"] == "Code Puppy CLI"
    assert payload["grant_types"] == [oauth.DEVICE_GRANT]
    assert payload["scope"] == "project:read project:write_token"
    assert "redirect_uris" not in payload


def test_authenticate_polls_and_saves(
    credentials: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        requests.append(request)
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "device_authorization_endpoint": "https://example.com/api/oauth/device/code",
                    "token_endpoint": "https://example.com/api/oauth/token",
                },
            )
        if request.url.path == "/api/oauth/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "device-secret",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://example.com/device",
                    "expires_in": 60,
                    "interval": 1,
                },
            )
        poll_count += 1
        if poll_count == 1:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(
            200,
            json={
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "project:read",
            },
        )

    monkeypatch.setattr(oauth.time, "sleep", lambda _: None)
    browser = Mock(return_value=True)
    announce = Mock()
    client = httpx.Client(transport=httpx.MockTransport(handler))

    tokens = oauth.authenticate(
        client=client,
        region="us",
        client_id="test-client",
        open_browser=browser,
        announce=announce,
    )

    assert tokens.access_token == "access-secret"
    assert oauth.load_tokens() == tokens
    browser.assert_called_once_with("https://example.com/device")
    assert poll_count == 2
    device_body = requests[1].content.decode()
    token_body = requests[2].content.decode()
    assert "code_challenge_method=S256" in device_body
    assert "code_verifier=" in token_body
    assert "device-secret" in token_body
    assert "access-secret" not in json.dumps([str(request.url) for request in requests])


def test_mint_write_token_selects_project_and_persists(
    credentials: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"organization_name": "pydantic", "project_name": "puppy"},
                    {"organization_name": "pydantic", "project_name": "platform"},
                ],
            )
        return httpx.Response(
            200,
            json={
                "token": "pylf-secret",
                "project_name": "puppy",
                "project_url": "https://example.com/pydantic/puppy",
            },
        )

    tokens = oauth.OAuthTokens(
        access_token="oauth-secret",
        refresh_token="refresh-secret",
        token_type="Bearer",
        expires_at=1234,
        scope=oauth.ONBOARD_SCOPES,
        base_url="https://example.com",
        client_id="client",
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    project = oauth.mint_write_token(tokens, project="pydantic/puppy", client=client)

    assert project.token == "pylf-secret"
    assert oauth.load_project_credentials() == project
    assert oauth.project_credential_path().stat().st_mode & 0o777 == 0o600
    assert requests[0].headers["authorization"] == "Bearer oauth-secret"
    assert requests[1].url.path.endswith("/pydantic/projects/puppy/write-tokens/")
    assert oauth.os.environ["LOGFIRE_TOKEN"] == "pylf-secret"
    monkeypatch.delenv("LOGFIRE_TOKEN")


def test_mint_write_token_requires_selection_for_multiple_projects() -> None:
    with pytest.raises(oauth.OAuthError, match="organization/project"):
        oauth._select_project(
            [
                {"organization_name": "one", "project_name": "project"},
                {"organization_name": "two", "project_name": "project"},
            ],
            None,
        )


def test_auth_command_onboards_when_project_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    onboard = Mock()
    authenticate = Mock()
    monkeypatch.setattr(callbacks, "load_project_credentials", lambda: None)
    monkeypatch.setattr(callbacks, "_onboard", onboard)
    monkeypatch.setattr(callbacks, "_authenticate", authenticate)

    assert callbacks._handle("/logfire auth eu", "logfire") is True
    onboard.assert_called_once_with("eu", None)
    authenticate.assert_not_called()


def test_auth_command_only_reauthenticates_when_project_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    onboard = Mock()
    authenticate = Mock()
    monkeypatch.setattr(
        callbacks, "load_project_credentials", Mock(return_value=object())
    )
    monkeypatch.setattr(callbacks, "_onboard", onboard)
    monkeypatch.setattr(callbacks, "_authenticate", authenticate)

    assert callbacks._handle("/logfire auth", "logfire") is True
    authenticate.assert_called_once_with(None)
    onboard.assert_not_called()


def test_authenticate_handles_flat_oauth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "device_authorization_endpoint": "https://example.com/device",
                    "token_endpoint": "https://example.com/token",
                },
            )
        return httpx.Response(
            400,
            json={
                "error": "invalid_scope",
                "error_description": "scope is not allowed",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(oauth.OAuthError, match="scope is not allowed"):
        oauth.authenticate(
            client=client, client_id="test-client", open_browser=lambda _: True
        )
