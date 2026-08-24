"""Read path: list sessions that have been mirrored into Logfire.

Listing goes through Logfire's hosted MCP ``query_run`` tool -- the exact same
path as the agent-facing ``logfire_query`` tool. Two consequences:

* The MCP layer scrubs keys/values containing sensitive keywords. The
  ``cp.hist.*`` namespace dodges key-based scrubbing; occasional value-level
  scrubbing in previews is cosmetic for a list view.
* Machine restore must NOT use this path (payloads would silently corrupt).
  Restore belongs behind a minted read token + ``/v2/query`` once the OAuth
  scope question is settled.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any

import httpx

LIST_SESSIONS_SQL = """
SELECT
  attributes->>'cp.hist.name'               AS name,
  attributes->>'cp.project.name'            AS project,
  attributes->>'cp.project.remote'          AS remote,
  count(DISTINCT attributes->>'cp.hist.seq') AS messages,
  max(start_timestamp)                      AS last_active
FROM records
WHERE attributes->>'cp.hist.name' IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY max(start_timestamp) DESC
LIMIT {limit}
"""


class SessionsQueryError(RuntimeError):
    """Raised when sessions cannot be listed from Logfire."""


def fresh_tokens() -> Any:
    """Return usable OAuth tokens, refreshing them when expired.

    Mirrors RFC 6749's refresh grant against the token endpoint discovered via
    RFC 8414 -- the same discovery the device flow uses. Refreshed tokens are
    persisted so other consumers stay warm.
    """
    from ..logfire_oauth.oauth import load_tokens, save_tokens

    tokens = load_tokens()
    if tokens is None:
        raise SessionsQueryError(
            "Logfire is not authenticated. Run /logfire auth first."
        )
    if tokens.expires_at > time.time() + 30:
        return tokens
    if not tokens.refresh_token:
        raise SessionsQueryError(
            "The Logfire OAuth access token expired and cannot be refreshed. "
            "Run /logfire auth again."
        )
    try:
        with httpx.Client(timeout=30) as client:
            metadata = client.get(
                f"{tokens.base_url}/.well-known/oauth-authorization-server"
            )
            metadata.raise_for_status()
            token_endpoint = metadata.json()["token_endpoint"]
            response = client.post(
                token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "client_id": tokens.client_id,
                    "refresh_token": tokens.refresh_token,
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise SessionsQueryError(
            f"Could not refresh the Logfire access token: {type(exc).__name__}"
        ) from exc

    tokens = dataclasses.replace(
        tokens,
        access_token=payload["access_token"],
        expires_at=time.time() + float(payload.get("expires_in", 300)),
        refresh_token=payload.get("refresh_token") or tokens.refresh_token,
    )
    save_tokens(tokens)
    return tokens


def list_sessions(limit: int = 25) -> list[dict[str, Any]]:
    """Return mirrored sessions ordered by most recent activity."""
    from ..logfire_oauth.oauth import load_project_credentials
    from ..logfire_oauth.query_tool import _query_mcp

    credentials = load_project_credentials()
    if credentials is None:
        raise SessionsQueryError(
            "No Logfire project is configured. Run /logfire onboard first."
        )

    tokens = fresh_tokens()
    result = _query_mcp(
        base_url=tokens.base_url,
        access_token=tokens.access_token,
        query=LIST_SESSIONS_SQL.format(limit=limit),
        project=credentials.project_name,
        start=None,
        end=None,
    )
    if isinstance(result, dict) and result.get("error"):
        raise SessionsQueryError(str(result["error"]))
    rows = result.get("rows") if isinstance(result, dict) else None
    if rows is None:
        raise SessionsQueryError("Unexpected response from the Logfire query API.")
    return rows
