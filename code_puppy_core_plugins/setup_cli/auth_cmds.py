"""``setup auth`` — one front door for every provider login Code Puppy has.

OAuth logins are *delegated* to the owning plugin's existing interactive
command in a full Code Puppy subprocess (``code-puppy -p "/x-auth"``):
those flows print through the messaging system and may open browsers or
localhost callbacks, none of which exist yet in the ``handle_cli_args``
context this CLI runs in. Status/logout reads go through the plugins'
own loaders with structured results. API keys are collected directly
(hidden prompt, stdin, or environment) into the shared credential
store and never echoed.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
from typing import Optional

from . import providers
from .output import Out, SetupError


def _delegate_slash_command(command: str) -> int:
    exe = shutil.which("code-puppy") or sys.argv[0]
    print(f'launching: code-puppy -p "/{command}" (interactive provider flow)')
    return subprocess.call([exe, "-p", f"/{command}"])


_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")


def _resolve_api_key_envs(provider_id: str) -> tuple[list[str], Optional[str]]:
    """Resolve an ``auth`` target to its API-key env vars.

    Accepts a models.dev provider id (``zai-coding-plan`` -> the env
    vars it declares, canonical first) or a raw env var name
    (``ZHIPU_API_KEY``). Returns ``(envs, models_dev_id)``; the id
    is None for the raw-env form. A known provider declaring no env
    vars yields ``([], id)``.
    """
    envs = providers.api_key_envs_for_provider(provider_id)
    if envs is not None:
        return envs, provider_id.strip().lower()
    return [provider_id.strip().upper()], None


def cmd_auth_list(out: Out) -> int:
    status = providers.all_provider_status()
    human = []
    for entry in status.oauth:
        mark = "authenticated" if entry["authenticated"] else "not authenticated"
        human.append(f"{entry['id']}: {mark} [{entry['sandbox_transport']}]")
    for entry in status.api_keys:
        if entry["authenticated"]:
            human.append(f"{entry['env_var']}: set")
    out.result(
        {"oauth": status.oauth, "api_keys": status.api_keys},
        human=human or ["(nothing authenticated)"],
    )
    return 0


def cmd_auth_status(out: Out, provider_id: str) -> int:
    provider = providers.get_oauth_provider(provider_id)
    if provider:
        entry = providers.oauth_status(provider)
        out.result(
            entry,
            human=f"{provider.id}: "
            + ("authenticated" if entry["authenticated"] else "not authenticated")
            + f" (source: {entry['source']})",
        )
        return 0 if entry["authenticated"] else 1
    envs, models_dev_id = _resolve_api_key_envs(provider_id)
    if models_dev_id is None and not providers.is_known_api_key_env(envs[0]):
        raise SetupError(
            f"unknown provider '{provider_id}' (see `setup auth list`)", exit_code=2
        )
    if not envs:
        out.result(
            {
                "provider": models_dev_id,
                "env_var": None,
                "env_vars": {},
                "authenticated": False,
                "kind": "api-key",
            },
            human=f"{models_dev_id}: declares no API-key env var in models.dev",
        )
        return 1
    per_env = {env: providers.credential_present(env) for env in envs}
    present = any(per_env.values())
    result: dict = {
        "env_var": envs[0],
        "env_vars": per_env,
        "authenticated": present,
        "kind": "api-key",
    }
    if models_dev_id:
        result["provider"] = models_dev_id
        detail = ", ".join(
            f"{env} {'set' if is_set else 'not set'}" for env, is_set in per_env.items()
        )
        human = f"{models_dev_id}: {'set' if present else 'not set'} ({detail})"
    else:
        human = f"{envs[0]}: {'set' if present else 'not set'}"
    out.result(result, human=human)
    return 0 if present else 1


def _collect_api_key(env_var: str, from_env: Optional[str], stdin: bool) -> str:
    if stdin:
        value = sys.stdin.read().strip()
    elif from_env:
        value = os.environ.get(from_env, "").strip()
    else:
        value = getpass.getpass(f"Enter {env_var}: ").strip()
    if not value:
        raise SetupError(f"no value provided for {env_var}")
    return value


def cmd_auth_login(
    out: Out, provider_id: str, from_env: Optional[str], stdin: bool
) -> int:
    provider = providers.get_oauth_provider(provider_id)
    if provider and provider.login_kind == "slash" and provider.auth_command:
        return _delegate_slash_command(provider.auth_command)
    if provider and provider.env_var:
        env_var, models_dev_id = provider.env_var, None
    else:
        envs, models_dev_id = _resolve_api_key_envs(provider_id)
        if models_dev_id is not None and not envs:
            raise SetupError(
                f"provider '{provider_id}' declares no API-key env var in models.dev",
                exit_code=2,
            )
        env_var = envs[0]
        if models_dev_id is None and not _ENV_NAME_RE.fullmatch(env_var):
            raise SetupError(
                f"unknown provider '{provider_id}' (see `setup auth list`"
                " and `setup model catalog`)",
                exit_code=2,
            )
    value = _collect_api_key(env_var, from_env, stdin)
    from code_puppy.provider_credentials import save_credential

    save_credential(env_var, value)
    result: dict = {"env_var": env_var, "saved": True}
    suffix = ""
    if models_dev_id:
        result["provider"] = models_dev_id
        suffix = f" (provider {models_dev_id})"
    out.result(result, human=f"saved credential for {env_var}{suffix}")
    return 0


def _clear_stored_credential(env_var: str) -> None:
    from pathlib import Path

    from code_puppy import config, secret_store, shared_credentials
    from code_puppy.atomic_io import path_lock
    from code_puppy.config_file import mutate_config

    # Core has no delete-credential API (save() rejects empty values), so
    # mirror shared_credentials.save() in reverse under the same lock:
    # drop the shared-store secret and any legacy puppy.cfg value.
    def _remove_legacy(parser):
        return (
            parser.remove_option(config.DEFAULT_SECTION, env_var)
            if parser.has_section(config.DEFAULT_SECTION)
            else False
        )

    try:
        with path_lock(str(Path(config.CONFIG_DIR) / "provider_credentials")):
            secret_store.delete_secret(shared_credentials._name(env_var))
            mutate_config(str(config.CONFIG_FILE), _remove_legacy)
    except Exception as e:
        raise SetupError(f"failed to clear stored credential for {env_var}: {e}") from e
    os.environ.pop(env_var, None)


def cmd_auth_logout(out: Out, provider_id: str) -> int:
    provider = providers.get_oauth_provider(provider_id)
    if provider and provider.logout_command:
        return _delegate_slash_command(provider.logout_command)
    if provider and provider.env_var:
        envs, models_dev_id = [provider.env_var], None
    else:
        envs, models_dev_id = _resolve_api_key_envs(provider_id)
    if models_dev_id is not None and not envs:
        raise SetupError(
            f"provider '{provider_id}' declares no API-key env var in models.dev",
            exit_code=2,
        )
    if models_dev_id is None and not _ENV_NAME_RE.fullmatch(envs[0]):
        raise SetupError(
            f"unknown provider '{provider_id}' (see `setup auth list`)", exit_code=2
        )
    for env_var in envs:
        _clear_stored_credential(env_var)
    result: dict = {"env_var": envs[0], "env_vars": envs, "cleared": True}
    if models_dev_id:
        result["provider"] = models_dev_id
    out.result(
        result,
        human=f"cleared stored credential for {', '.join(envs)} "
        "(a shell environment variable of the same name still wins if set)",
    )
    return 0
