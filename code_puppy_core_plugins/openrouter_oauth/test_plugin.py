"""Tests for the OpenRouter OAuth PKCE plugin."""

from __future__ import annotations

import base64
import hashlib
import threading
import urllib.error
import urllib.parse
import urllib.request
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from code_puppy_core_plugins.openrouter_oauth import oauth_flow, register_callbacks
from code_puppy_core_plugins.openrouter_oauth.config import OPENROUTER_OAUTH_CONFIG


# -- PKCE --------------------------------------------------------------------


class TestPkce:
    def test_challenge_is_s256_of_verifier(self):
        verifier, challenge = oauth_flow.generate_pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert challenge == expected
        assert "=" not in challenge

    def test_pairs_are_unique(self):
        assert oauth_flow.generate_pkce_pair() != oauth_flow.generate_pkce_pair()


# -- code -> key exchange ----------------------------------------------------


class TestExchange:
    def _response(self, payload, status=200):
        response = MagicMock()
        response.json.return_value = payload
        if status >= 400:
            response.raise_for_status.side_effect = Exception(f"HTTP {status}")
        return response

    def test_success_returns_key(self):
        with patch.object(oauth_flow.requests, "post") as mock_post:
            mock_post.return_value = self._response({"key": "sk-or-v1-abc"})
            key = oauth_flow.exchange_code_for_key("code123", "verifier456")
        assert key == "sk-or-v1-abc"
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {
            "code": "code123",
            "code_verifier": "verifier456",
            "code_challenge_method": "S256",
        }
        assert mock_post.call_args[0][0] == OPENROUTER_OAUTH_CONFIG["keys_url"]

    def test_missing_key_raises(self):
        with patch.object(oauth_flow.requests, "post") as mock_post:
            mock_post.return_value = self._response({"nope": True})
            with pytest.raises(RuntimeError, match="did not include an API key"):
                oauth_flow.exchange_code_for_key("code", "verifier")

    def test_http_error_propagates(self):
        with patch.object(oauth_flow.requests, "post") as mock_post:
            mock_post.return_value = self._response({}, status=403)
            with pytest.raises(Exception, match="HTTP 403"):
                oauth_flow.exchange_code_for_key("code", "verifier")


# -- localhost callback server -----------------------------------------------


class TestCallbackServer:
    def _serve(self):
        server = oauth_flow._OAuthServer()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _get(self, url):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_auth_url_contains_callback_and_challenge(self):
        server = oauth_flow._OAuthServer()
        try:
            url = server.auth_url()
            assert url.startswith(OPENROUTER_OAUTH_CONFIG["auth_url"])
            assert urllib.parse.quote(server.callback_url, safe="") in url
            assert "code_challenge_method=S256" in url
        finally:
            server.server_close()

    def test_callback_with_code_saves_key(self):
        server, thread = self._serve()
        try:
            with (
                patch.object(oauth_flow, "exchange_code_for_key") as mock_exchange,
                patch.object(oauth_flow, "save_credential") as mock_save,
            ):
                mock_exchange.return_value = "sk-or-v1-abc"
                status = self._get(f"{server.callback_url}?code=authcode")
            assert status == 200
            assert server.exit_code == 0
            mock_exchange.assert_called_once_with("authcode", server.code_verifier)
            mock_save.assert_called_once_with(
                OPENROUTER_OAUTH_CONFIG["env_var"], "sk-or-v1-abc"
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)

    def test_callback_missing_code_fails(self):
        server, thread = self._serve()
        try:
            assert self._get(server.callback_url) == 400
            assert server.exit_code == 1
        finally:
            server.shutdown()
            thread.join(timeout=5)

    def test_callback_wrong_path_404s(self):
        server, thread = self._serve()
        try:
            host, port = server.server_address[:2]
            assert self._get(f"http://{host}:{port}/nope?code=x") == 404
        finally:
            server.shutdown()
            thread.join(timeout=5)

    def test_callback_provider_error_fails(self):
        server, thread = self._serve()
        try:
            status = self._get(
                f"{server.callback_url}?error=access_denied"
                "&error_description=user+said+no"
            )
            assert status == 400
            assert server.exit_code == 1
        finally:
            server.shutdown()
            thread.join(timeout=5)


# -- pasted input ------------------------------------------------------------


class TestPastedInput:
    def test_full_url_paste_exchanges_code(self):
        with (
            patch.object(oauth_flow, "exchange_code_for_key") as mock_exchange,
            patch.object(oauth_flow, "save_credential") as mock_save,
        ):
            mock_exchange.return_value = "sk-or-v1-abc"
            ok = oauth_flow._complete_pasted_input(
                "http://localhost:5555/callback?code=pasted", "verifier"
            )
        assert ok is True
        mock_exchange.assert_called_once_with("pasted", "verifier")
        mock_save.assert_called_once()

    def test_error_paste_fails(self):
        ok = oauth_flow._complete_pasted_input(
            "?error=access_denied&error_description=nope", "verifier"
        )
        assert ok is False

    def test_exchange_failure_reported(self):
        with patch.object(oauth_flow, "exchange_code_for_key") as mock_exchange:
            mock_exchange.side_effect = RuntimeError("boom")
            assert oauth_flow._complete_pasted_input("barecode", "verifier") is False


# -- credential-flow hook ----------------------------------------------------


class TestCredentialFlowHook:
    def test_ignores_other_providers(self):
        with patch.object(register_callbacks, "choose_flow") as mock_choose:
            assert (
                register_callbacks._credential_flow(
                    provider_id="acme", env_var="ACME_API_KEY"
                )
                is None
            )
        mock_choose.assert_not_called()

    def test_ignores_other_env_vars(self):
        with patch.object(register_callbacks, "choose_flow") as mock_choose:
            assert (
                register_callbacks._credential_flow(
                    provider_id="openrouter", env_var="SOMETHING_ELSE"
                )
                is None
            )
        mock_choose.assert_not_called()

    def test_manual_choice_defers(self):
        with (
            patch.object(register_callbacks, "choose_flow", return_value="manual"),
            patch.object(oauth_flow, "run_oauth_flow") as mock_run,
        ):
            assert (
                register_callbacks._credential_flow(
                    provider_id="openrouter", env_var="OPENROUTER_API_KEY"
                )
                is None
            )
        mock_run.assert_not_called()

    def test_oauth_success_returns_true(self):
        with (
            patch.object(register_callbacks, "choose_flow", return_value="oauth"),
            patch.object(oauth_flow, "run_oauth_flow", return_value=True),
        ):
            assert (
                register_callbacks._credential_flow(
                    provider_id="openrouter", env_var="OPENROUTER_API_KEY"
                )
                is True
            )

    def test_oauth_failure_defers_to_manual(self):
        with (
            patch.object(register_callbacks, "choose_flow", return_value="oauth"),
            patch.object(oauth_flow, "run_oauth_flow", return_value=False),
        ):
            assert (
                register_callbacks._credential_flow(
                    provider_id="openrouter", env_var="OPENROUTER_API_KEY"
                )
                is None
            )


# -- choice menu (headless) --------------------------------------------------


class TestChoiceMenu:
    def _keys(self, *keys):
        script = iter(keys)
        return {
            "key_source": lambda: next(script),
            "output": StringIO(),
            "size": lambda: (90, 20),
        }

    def test_enter_selects_oauth(self):
        assert register_callbacks.choose_flow(**self._keys("enter")) == "oauth"

    def test_down_enter_selects_manual(self):
        assert register_callbacks.choose_flow(**self._keys("down", "enter")) == "manual"

    def test_escape_defers_to_manual(self):
        assert register_callbacks.choose_flow(**self._keys("escape")) == "manual"
