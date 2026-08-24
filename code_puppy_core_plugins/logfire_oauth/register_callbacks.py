"""Register the Logfire OAuth slash command."""

from __future__ import annotations

import time

from code_puppy.callbacks import register_callback
from code_puppy.messaging import emit_error, emit_info, emit_success, emit_warning

from .query_tool import register_logfire_query
from .oauth import (
    ONBOARD_SCOPES,
    OAuthError,
    OAuthTokens,
    authenticate,
    delete_project_credentials,
    delete_tokens,
    load_project_credentials,
    load_tokens,
    mint_write_token,
)


def _register_tools():
    return [{"name": "logfire_query", "register_func": register_logfire_query}]


def _advertise_tools(agent_name=None):
    del agent_name
    return ["logfire_query"]


def _help() -> list[tuple[str, str]]:
    return [("logfire", "Authenticate Code Puppy with Pydantic Logfire")]


def _usage() -> None:
    emit_info(
        "Logfire commands: /logfire auth [us|eu], "
        "/logfire onboard [organization/project] [us|eu], "
        "/logfire status, /logfire logout"
    )


def _authenticate(
    region: str | None, *, scopes: str | None = None
) -> OAuthTokens | None:
    try:
        kwargs = {"region": region, "announce": emit_info}
        if scopes is not None:
            kwargs["scopes"] = scopes
        tokens = authenticate(**kwargs)
    except OAuthError as exc:
        emit_error(str(exc))
        return None
    emit_success("Code Puppy is authenticated with Logfire.")
    return tokens


def _onboard(region: str | None, project: str | None) -> None:
    tokens = _authenticate(region, scopes=ONBOARD_SCOPES)
    if tokens is None:
        return
    try:
        credentials = mint_write_token(tokens, project=project)
    except OAuthError as exc:
        emit_error(str(exc))
        return
    from code_puppy.config import set_value
    from code_puppy.observability import configure_logfire

    set_value("enable_logfire", "true")
    configure_logfire()
    emit_success(
        f"Minted a write token for Logfire project {credentials.project_name}."
    )
    emit_info(f"Project: {credentials.project_url}")
    emit_info(
        "Logfire observability is enabled; the token is saved for future Code Puppy runs."
    )


def _status() -> None:
    tokens = load_tokens()
    credentials = load_project_credentials()
    if tokens is None:
        emit_warning("Code Puppy is not authenticated with Logfire.")
        return
    state = "expired" if tokens.expires_at <= time.time() else "authenticated"
    emit_info(f"Logfire OAuth: {state}")
    emit_info(f"Server: {tokens.base_url}")
    emit_info(f"Scopes: {tokens.scope or '(not returned by server)'}")
    if tokens.refresh_token:
        emit_info("Refresh credential: available")
    if credentials:
        emit_info(f"Write-token project: {credentials.project_name}")
        emit_info(f"Project URL: {credentials.project_url}")
    else:
        emit_warning("Write token: not configured; run /logfire onboard")


def _startup() -> None:
    """Make a previously minted write token available to Logfire configuration."""
    import os

    credentials = load_project_credentials()
    if credentials and "LOGFIRE_TOKEN" not in os.environ:
        os.environ["LOGFIRE_TOKEN"] = credentials.token


def _handle(command: str, name: str) -> bool | None:
    if name not in {"logfire", "logfire/auth", "logfire/status", "logfire/logout"}:
        return None

    parts = command.lstrip("/").split()
    if name.startswith("logfire/"):
        action = name.partition("/")[2]
        args = parts[1:]
    else:
        action = parts[1].lower() if len(parts) > 1 else "status"
        args = parts[2:]

    if action == "auth":
        region = args[0].lower() if args else None
        if region not in {None, "us", "eu"}:
            emit_warning("Region must be 'us' or 'eu'.")
            return True
        if load_project_credentials() is None:
            emit_info("No Logfire project is configured; continuing with onboarding.")
            _onboard(region, None)
        else:
            _authenticate(region)
        return True
    if action == "onboard":
        region = next(
            (arg.lower() for arg in args if arg.lower() in {"us", "eu"}), None
        )
        project_args = [arg for arg in args if arg.lower() not in {"us", "eu"}]
        if len(project_args) > 1 or (project_args and "/" not in project_args[0]):
            _usage()
            return True
        _onboard(region, project_args[0] if project_args else None)
        return True
    if action == "status":
        _status()
        return True
    if action == "logout":
        delete_project_credentials()
        if delete_tokens():
            emit_success("Removed Code Puppy Logfire OAuth credentials.")
        else:
            emit_info("Code Puppy had no Logfire OAuth credentials to remove.")
        return True

    _usage()
    return True


# Plugin modules load before core observability is configured. Restore the token
# immediately; the startup callback remains as a defensive idempotent fallback.
_startup()
register_callback("startup", _startup)
register_callback("custom_command_help", _help)
register_callback("custom_command", _handle)
register_callback("register_tools", _register_tools)
register_callback("register_agent_tools", _advertise_tools)
