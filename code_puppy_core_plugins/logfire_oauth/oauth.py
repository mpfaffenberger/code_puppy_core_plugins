"""RFC 8628 OAuth device flow for Pydantic Logfire."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

DEFAULT_BASE_URLS = {
    "us": "https://logfire-us.pydantic.dev",
    "eu": "https://logfire-eu.pydantic.dev",
}
DEFAULT_SCOPES = "project:read"
ONBOARD_SCOPES = f"{DEFAULT_SCOPES} project:write_token"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class OAuthError(RuntimeError):
    """A safe, user-facing OAuth failure."""


@dataclass(frozen=True, slots=True)
class ProjectCredentials:
    token: str
    project_name: str
    project_url: str
    logfire_api_url: str

    @classmethod
    def from_response(
        cls, payload: dict[str, Any], *, base_url: str
    ) -> "ProjectCredentials":
        try:
            return cls(
                token=str(payload["token"]),
                project_name=str(payload["project_name"]),
                project_url=str(payload["project_url"]),
                logfire_api_url=base_url,
            )
        except KeyError as exc:
            raise OAuthError(
                f"Logfire write-token response omitted {exc.args[0]}"
            ) from None

    def as_dict(self) -> dict[str, str]:
        return {
            "token": self.token,
            "project_name": self.project_name,
            "project_url": self.project_url,
            "logfire_api_url": self.logfire_api_url,
        }


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    access_token: str
    refresh_token: str | None
    token_type: str
    expires_at: float
    scope: str
    base_url: str
    client_id: str

    @classmethod
    def from_response(
        cls, payload: dict[str, Any], *, base_url: str, client_id: str
    ) -> "OAuthTokens":
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OAuthError("Logfire returned no access token")
        expires_in = payload.get("expires_in", 3600)
        if not isinstance(expires_in, (int, float)):
            expires_in = 3600
        return cls(
            access_token=access_token,
            refresh_token=_optional_string(payload.get("refresh_token")),
            token_type=str(payload.get("token_type", "Bearer")),
            expires_at=time.time() + float(expires_in),
            scope=str(payload.get("scope", "")),
            base_url=base_url,
            client_id=client_id,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "base_url": self.base_url,
            "client_id": self.client_id,
        }


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def project_credential_path() -> Path:
    configured = os.environ.get("CODE_PUPPY_LOGFIRE_PROJECT_CREDENTIALS")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".code_puppy" / "logfire_project.json"
    )


def credential_path() -> Path:
    configured = os.environ.get("CODE_PUPPY_LOGFIRE_CREDENTIALS")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".code_puppy" / "logfire_oauth.json"
    )


def save_tokens(tokens: OAuthTokens, path: Path | None = None) -> None:
    """Atomically persist credentials in a user-only file."""
    path = path or credential_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(tokens.as_dict(), file)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(path)
    path.chmod(0o600)


def save_project_credentials(
    credentials: ProjectCredentials, path: Path | None = None
) -> None:
    path = path or project_credential_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(credentials.as_dict(), file)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(path)
    path.chmod(0o600)


def load_project_credentials(path: Path | None = None) -> ProjectCredentials | None:
    path = path or project_credential_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ProjectCredentials(
            token=payload["token"],
            project_name=payload["project_name"],
            project_url=payload["project_url"],
            logfire_api_url=payload["logfire_api_url"],
        )
    except (FileNotFoundError, KeyError, TypeError, json.JSONDecodeError):
        return None


def load_tokens(path: Path | None = None) -> OAuthTokens | None:
    path = path or credential_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return OAuthTokens(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            token_type=payload.get("token_type", "Bearer"),
            expires_at=float(payload["expires_at"]),
            scope=payload.get("scope", ""),
            base_url=payload["base_url"],
            client_id=payload["client_id"],
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def delete_project_credentials(path: Path | None = None) -> bool:
    path = path or project_credential_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def delete_tokens(path: Path | None = None) -> bool:
    path = path or credential_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def base_url_for(region: str | None = None) -> str:
    configured = os.environ.get("CODE_PUPPY_LOGFIRE_BASE_URL") or os.environ.get(
        "LOGFIRE_BASE_URL"
    )
    if configured:
        return configured.rstrip("/")
    selected = region or os.environ.get("CODE_PUPPY_LOGFIRE_REGION", "us")
    try:
        return DEFAULT_BASE_URLS[selected.lower()]
    except KeyError:
        raise OAuthError(
            f"Unknown Logfire region: {selected!r}; expected 'us' or 'eu'"
        ) from None


def register_dynamic_client(
    client: httpx.Client, metadata: dict[str, Any], scopes: str
) -> str:
    endpoint = metadata.get("registration_endpoint")
    if not isinstance(endpoint, str):
        raise OAuthError("Logfire OAuth discovery omitted registration_endpoint")
    response = client.post(
        endpoint,
        json={
            "client_name": "Code Puppy CLI",
            "client_uri": "https://github.com/mpfaffenberger/code_puppy",
            "grant_types": [DEVICE_GRANT],
            "token_endpoint_auth_method": "none",
            "application_type": "native",
            "scope": scopes,
        },
        headers={"User-Agent": "code-puppy"},
    )
    if response.is_error:
        _, description = _oauth_error(response)
        raise OAuthError(f"Logfire OAuth client registration failed: {description}")
    client_id = response.json().get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise OAuthError("Logfire returned no client ID after registration")
    return client_id


def discover(client: httpx.Client, base_url: str) -> dict[str, Any]:
    response = client.get(f"{base_url}/.well-known/oauth-authorization-server")
    response.raise_for_status()
    metadata = response.json()
    for key in ("device_authorization_endpoint", "token_endpoint"):
        if not isinstance(metadata.get(key), str):
            raise OAuthError(f"Logfire OAuth discovery omitted {key}")
    return metadata


def _oauth_error(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except ValueError:
        return "server_error", f"HTTP {response.status_code}"
    detail = payload.get("detail", payload) if isinstance(payload, dict) else {}
    if not isinstance(detail, dict):
        return "server_error", f"HTTP {response.status_code}"
    code = str(detail.get("error", "server_error"))
    description = str(detail.get("error_description", code))
    return code, description


def mint_write_token(
    tokens: OAuthTokens,
    *,
    project: str | None = None,
    client: httpx.Client | None = None,
) -> ProjectCredentials:
    """Select an accessible project and mint its Logfire write token."""
    owned_client = client is None
    client = client or httpx.Client(timeout=15)
    headers = {"Authorization": f"Bearer {tokens.access_token}"}
    try:
        response = client.get(
            f"{tokens.base_url}/v1/writable-projects/", headers=headers
        )
        response.raise_for_status()
        projects = response.json()
        if not isinstance(projects, list):
            raise OAuthError("Logfire returned an invalid project list")
        selected = _select_project(projects, project)
        organization = selected["organization_name"]
        project_name = selected["project_name"]
        response = client.post(
            f"{tokens.base_url}/v1/organizations/{organization}/projects/{project_name}/write-tokens/",
            headers=headers,
        )
        if response.is_error:
            _, description = _oauth_error(response)
            raise OAuthError(f"Could not mint a Logfire write token: {description}")
        credentials = ProjectCredentials.from_response(
            response.json(), base_url=tokens.base_url
        )
        save_project_credentials(credentials)
        os.environ["LOGFIRE_TOKEN"] = credentials.token
        return credentials
    except httpx.HTTPError as exc:
        raise OAuthError(
            f"Could not configure a Logfire project: {type(exc).__name__}"
        ) from exc
    finally:
        if owned_client:
            client.close()


def _select_project(projects: list[Any], requested: str | None) -> dict[str, str]:
    valid = [
        project
        for project in projects
        if isinstance(project, dict)
        and isinstance(project.get("organization_name"), str)
        and isinstance(project.get("project_name"), str)
    ]
    if requested:
        matches = [
            project
            for project in valid
            if f"{project['organization_name']}/{project['project_name']}" == requested
        ]
        if len(matches) == 1:
            return matches[0]
        raise OAuthError(f"Logfire project {requested!r} is not available")
    if len(valid) == 1:
        return valid[0]
    if not valid:
        raise OAuthError("No writable Logfire projects are available")
    choices = ", ".join(
        f"{item['organization_name']}/{item['project_name']}" for item in valid
    )
    raise OAuthError(
        "More than one Logfire project is available. Run "
        f"/logfire onboard <organization/project>. Choices: {choices}"
    )


def authenticate(
    *,
    region: str | None = None,
    client_id: str | None = None,
    scopes: str = DEFAULT_SCOPES,
    open_browser: Callable[[str], Any] = webbrowser.open,
    announce: Callable[[str], None] = print,
    client: httpx.Client | None = None,
) -> OAuthTokens:
    """Run Logfire's RFC 8628 device authorization grant synchronously."""
    base_url = base_url_for(region)
    configured_client_id = client_id or os.environ.get("CODE_PUPPY_LOGFIRE_CLIENT_ID")
    owned_client = client is None
    client = client or httpx.Client(timeout=15, follow_redirects=False)
    try:
        metadata = discover(client, base_url)
        if configured_client_id:
            client_id = configured_client_id
        else:
            stored = load_tokens()
            client_id = (
                stored.client_id
                if stored is not None and stored.base_url == base_url
                else register_dynamic_client(client, metadata, scopes)
            )
        verifier, challenge = generate_pkce()
        response = client.post(
            metadata["device_authorization_endpoint"],
            data={
                "client_id": client_id,
                "scope": scopes,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        if response.is_error:
            _, description = _oauth_error(response)
            raise OAuthError(f"Logfire rejected device authorization: {description}")
        device = response.json()
        device_code = device.get("device_code")
        verification_url = device.get("verification_uri_complete") or device.get(
            "verification_uri"
        )
        if not isinstance(device_code, str) or not isinstance(verification_url, str):
            raise OAuthError(
                "Logfire returned an invalid device authorization response"
            )

        user_code = device.get("user_code")
        announce(f"Open {verification_url}")
        if user_code and not device.get("verification_uri_complete"):
            announce(f"Enter code: {user_code}")
        if not open_browser(verification_url):
            announce("The browser did not open automatically; use the URL above.")

        interval = max(float(device.get("interval", 5)), 1)
        deadline = time.monotonic() + float(device.get("expires_in", 600))
        while time.monotonic() < deadline:
            time.sleep(interval)
            token_response = client.post(
                metadata["token_endpoint"],
                data={
                    "grant_type": DEVICE_GRANT,
                    "device_code": device_code,
                    "client_id": client_id,
                    "code_verifier": verifier,
                },
            )
            if not token_response.is_error:
                tokens = OAuthTokens.from_response(
                    token_response.json(), base_url=base_url, client_id=client_id
                )
                save_tokens(tokens)
                return tokens
            code, description = _oauth_error(token_response)
            if code == "authorization_pending":
                continue
            if code == "slow_down":
                interval += 5
                continue
            raise OAuthError(f"Logfire authorization failed: {description}")
        raise OAuthError("Logfire authorization expired before it was approved")
    except httpx.HTTPError as exc:
        raise OAuthError(f"Could not reach Logfire: {type(exc).__name__}") from exc
    finally:
        if owned_client:
            client.close()
