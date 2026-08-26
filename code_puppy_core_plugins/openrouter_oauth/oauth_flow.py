"""OpenRouter OAuth PKCE flow.

Follows https://openrouter.ai/docs/use-cases/oauth-pkce: send the user to
``/auth`` with an S256 code challenge, receive the authorization code on a
localhost callback (any port is allowed), then exchange code + verifier at
``/api/v1/auth/keys`` for a user-controlled API key. There is no state
parameter in OpenRouter's flow -- PKCE is the integrity mechanism, and the
code is single-use with a 10 minute expiry.

The key is persisted via :func:`code_puppy.provider_credentials.save_credential`
so it is immediately usable by the model factory (puppy.cfg + os.environ).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Tuple

import requests

from code_puppy.messaging import emit_error, emit_info, emit_success, emit_warning
from code_puppy.provider_credentials import save_credential

from ..oauth_pasteback import parse_oauth_callback_input, read_available_stdin_line
from ..oauth_puppy_html import oauth_failure_html, oauth_success_html
from .config import OPENROUTER_OAUTH_CONFIG


def generate_pkce_pair() -> Tuple[str, str]:
    """Return an S256 ``(code_verifier, code_challenge)`` pair."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def exchange_code_for_key(code: str, code_verifier: str) -> str:
    """Exchange an authorization code for a user-controlled API key.

    Raises on HTTP errors or a malformed response so callers can surface
    the failure; returns the API key on success.
    """
    response = requests.post(
        OPENROUTER_OAUTH_CONFIG["keys_url"],
        json={
            "code": code,
            "code_verifier": code_verifier,
            "code_challenge_method": "S256",
        },
        timeout=30,
    )
    response.raise_for_status()
    key = response.json().get("key")
    if not key:
        raise RuntimeError("OpenRouter response did not include an API key")
    return key


def _save_key(key: str) -> None:
    save_credential(OPENROUTER_OAUTH_CONFIG["env_var"], key)


class _OAuthServer(HTTPServer):
    """Localhost callback server holding the state for one OAuth attempt."""

    def __init__(self) -> None:
        host = OPENROUTER_OAUTH_CONFIG["redirect_host"]
        # OpenRouter explicitly supports localhost callbacks on any port.
        super().__init__((host, 0), _CallbackHandler)

        self.exit_code = 1
        self.code_verifier, self.code_challenge = generate_pkce_pair()
        port = self.server_address[1]
        path = OPENROUTER_OAUTH_CONFIG["redirect_path"]
        self.callback_url = f"http://{host}:{port}{path}"

    def auth_url(self) -> str:
        params = {
            "callback_url": self.callback_url,
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{OPENROUTER_OAUTH_CONFIG['auth_url']}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code: str) -> None:
        _save_key(exchange_code_for_key(code, self.code_verifier))


class _CallbackHandler(BaseHTTPRequestHandler):
    server: "_OAuthServer"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != OPENROUTER_OAUTH_CONFIG["redirect_path"]:
            self._send_failure(404, "Callback endpoint not found for the puppy parade.")
            return

        params = urllib.parse.parse_qs(parsed.query)
        error = params.get("error", [None])[0]
        if error:
            description = params.get("error_description", [error])[0]
            self._send_failure(400, f"OpenRouter authorization failed: {description}")
            self._shutdown_after_delay()
            return

        code = params.get("code", [None])[0]
        if not code:
            self._send_failure(400, "Missing auth code — the token treat rolled away.")
            self._shutdown_after_delay()
            return

        try:
            self.server.exchange_code(code)
        except Exception as exc:  # noqa: BLE001
            self._send_failure(500, f"Key exchange failed: {exc}")
            self._shutdown_after_delay()
            return

        self.server.exit_code = 0
        self._send_html(
            oauth_success_html(
                "OpenRouter",
                "You can now close this window and return to Code Puppy.",
            )
        )
        self._shutdown_after_delay()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass

    def _send_html(self, body: str, status: int = 200) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_failure(self, status: int, reason: str) -> None:
        self._send_html(oauth_failure_html("OpenRouter", reason), status)

    def _shutdown_after_delay(self, seconds: float = 2.0) -> None:
        def _later() -> None:
            time.sleep(seconds)
            self.server.shutdown()

        threading.Thread(target=_later, daemon=True).start()


def _complete_pasted_input(raw_input: str, code_verifier: str) -> bool:
    """Exchange a pasted callback URL / bare code for an API key."""
    try:
        parsed = parse_oauth_callback_input(raw_input)
    except ValueError as exc:
        emit_error(f"Could not parse pasted OAuth input: {exc}")
        return False

    if parsed.error:
        emit_error(f"OpenRouter returned an error: {parsed.error_message}")
        return False
    if not parsed.code:
        emit_error("Pasted OAuth input did not contain an authorization code.")
        return False

    try:
        _save_key(exchange_code_for_key(parsed.code, code_verifier))
    except Exception as exc:  # noqa: BLE001
        emit_error(f"Key exchange failed: {exc}")
        return False
    return True


def _wait_for_completion(server: _OAuthServer) -> bool:
    """Poll for the localhost callback, accepting pasted input meanwhile."""
    emit_info(
        "Waiting for the OpenRouter callback. If localhost cannot be reached "
        "(SSH, container), paste the full callback URL or authorization code "
        "here and press Enter."
    )

    elapsed = 0.0
    timeout = OPENROUTER_OAUTH_CONFIG["callback_timeout"]
    interval = 0.25
    while elapsed < timeout:
        if server.exit_code == 0:
            return True

        pasted = read_available_stdin_line()
        if pasted is not None and pasted.strip():
            if _complete_pasted_input(pasted, server.code_verifier):
                return True

        time.sleep(interval)
        elapsed += interval
    return False


def run_oauth_flow() -> bool:
    """Run the browser PKCE flow. Returns True when a key was saved."""
    try:
        server = _OAuthServer()
    except Exception as exc:  # noqa: BLE001
        emit_error(f"Could not start OpenRouter OAuth flow: {exc}")
        return False

    auth_url = server.auth_url()
    emit_info(f"Open this URL in your browser: {auth_url}")

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    from code_puppy.tools.common import should_suppress_browser

    if should_suppress_browser():
        emit_info(f"[HEADLESS MODE] Would normally open: {auth_url}")
    else:
        try:
            import webbrowser

            if not webbrowser.open(auth_url):
                emit_warning("Please open the URL manually — the browser stayed home.")
        except Exception as exc:  # noqa: BLE001
            emit_warning(f"Could not open browser automatically: {exc}")

    completed = _wait_for_completion(server)

    server.shutdown()
    server_thread.join(timeout=5)

    if not completed:
        emit_error("OpenRouter authentication failed or timed out.")
        return False

    env_var = OPENROUTER_OAUTH_CONFIG["env_var"]
    emit_success(f"OpenRouter OAuth complete — API key saved as {env_var}.")
    return True
