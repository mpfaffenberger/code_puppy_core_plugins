"""``setup sbx plan`` — derive sandbox needs from the configured catalog.

The Docker sandbox kits must ask only for credentials the user
actually uses. Hand-enumerating providers in kit YAML both nags about
unused keys and misses long-tail setups (ZAI Coding Plan, any
``custom_openai`` endpoint added via models.dev). This module walks
the merged model catalog and derives, per configured model:

- the credential env var and the domain/header the proxy must inject,
- the egress domains the sandbox needs,
- or the OAuth transport when no static key exists.

Values are never read or emitted — only presence on the host.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Optional
from urllib.parse import urlparse

from . import providers
from .output import Out, SetupError

# Static-key model types: type -> (service, env var, domain, header, format)
_STATIC_TYPES: dict[str, tuple[str, str, str, str, str]] = {
    "meta_oauth": ("meta", "META_API_KEY", "api.meta.ai", "Authorization", "Bearer %s"),
    "anthropic": (
        "anthropic",
        "ANTHROPIC_API_KEY",
        "api.anthropic.com",
        "x-api-key",
        "%s",
    ),
    "openai": (
        "openai",
        "OPENAI_API_KEY",
        "api.openai.com",
        "Authorization",
        "Bearer %s",
    ),
    "gemini": (
        "gemini",
        "GEMINI_API_KEY",
        "generativelanguage.googleapis.com",
        "x-goog-api-key",
        "%s",
    ),
    "openrouter": (
        "openrouter",
        "OPENROUTER_API_KEY",
        "openrouter.ai",
        "Authorization",
        "Bearer %s",
    ),
    "zai_coding": ("zai", "ZAI_API_KEY", "api.z.ai", "Authorization", "Bearer %s"),
    "zai_api": ("zai", "ZAI_API_KEY", "api.z.ai", "Authorization", "Bearer %s"),
    "cerebras": (
        "cerebras",
        "CEREBRAS_API_KEY",
        "api.cerebras.ai",
        "Authorization",
        "Bearer %s",
    ),
}

_CUSTOM_TYPES = {
    "custom_openai": ("Authorization", "Bearer %s"),
    "custom_openai_responses": ("Authorization", "Bearer %s"),
    "custom_anthropic": ("x-api-key", "%s"),
    "custom_gemini": ("x-goog-api-key", "%s"),
}

# OAuth model types: type -> (providers.py id, egress domains)
_OAUTH_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "claude_code": (
        "claude-code",
        (
            "api.anthropic.com",
            "platform.claude.com",
            "claude.ai",
            "console.anthropic.com",
        ),
    ),
    "chatgpt_oauth": ("codex", ("chatgpt.com", "auth.openai.com")),
    "copilot": ("copilot", ("api.githubcopilot.com", "api.github.com", "github.com")),
    "grok_oauth": ("grok", ("api.x.ai", "auth.x.ai", "accounts.x.ai")),
}

_UNSUPPORTED_TYPES = {
    "azure_openai": "per-deployment host; add its domain and AZURE_OPENAI_API_KEY handling manually",
    "azure_foundry_openai": "per-deployment host; configure manually",
    "round_robin": "meta-type over other models; covered via its members if configured",
}


def _service_from_env(env_var: str) -> str:
    name = env_var.lower()
    for suffix in ("_api_key", "_key", "_token"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return re.sub(r"[^a-z0-9-]", "-", name) or "custom"


def _custom_endpoint_credential(
    model_key: str, cfg: dict[str, Any], header: str, fmt: str
) -> Optional[dict[str, Any]]:
    endpoint = cfg.get("custom_endpoint")
    if not isinstance(endpoint, dict):
        return None
    host = urlparse(str(endpoint.get("url") or "")).hostname
    # Env-var extraction is core's own logic (same $ENV grammar the
    # factory resolves), so new reference shapes need no change here.
    try:
        from code_puppy.provider_credentials import (
            extract_api_key_env_vars_from_model_config,
        )

        names = extract_api_key_env_vars_from_model_config(cfg)
        env_var = names[0] if names else ""
    except Exception:
        raw_key = str(endpoint.get("api_key") or "")
        env_var = raw_key[1:] if raw_key.startswith("$") else ""
    if not host or not env_var:
        return None
    return {
        "service": _service_from_env(env_var),
        "env_var": env_var,
        "domain": host,
        "header": header,
        "format": fmt,
        "models": [model_key],
    }


def build_plan(
    catalog: Optional[dict[str, Any]] = None, provider: Optional[str] = None
) -> dict[str, Any]:
    """Derive the sandbox credential/egress plan from a model catalog.

    ``provider`` optionally restricts the result to one service/provider
    id, matching the sbx wizard's --provider filter.
    """
    if catalog is None:
        from code_puppy.model_factory import ModelFactory

        catalog = ModelFactory.load_config()

    credentials: dict[tuple[str, str, str], dict[str, Any]] = {}
    oauth: dict[str, dict[str, Any]] = {}
    unsupported: list[dict[str, Any]] = []
    egress: set[str] = set()

    def add_credential(entry: dict[str, Any]) -> None:
        key = (entry["service"], entry["env_var"], entry["domain"])
        if key in credentials:
            credentials[key]["models"].extend(entry["models"])
        else:
            credentials[key] = entry
        egress.add(entry["domain"])

    for model_key in sorted(catalog):
        cfg = catalog[model_key]
        if not isinstance(cfg, dict):
            continue
        model_type = str(cfg.get("type") or "")

        if model_type in _OAUTH_TYPES:
            provider_id, domains = _OAUTH_TYPES[model_type]
            oauth_provider = providers.get_oauth_provider(provider_id)
            oauth.setdefault(
                provider_id,
                {
                    "id": provider_id,
                    "transport": oauth_provider.sandbox_transport
                    if oauth_provider
                    else "token-file-sync",
                    "domains": list(domains),
                    "models": [],
                },
            )["models"].append(model_key)
            egress.update(domains)
            continue

        if model_type in _CUSTOM_TYPES or (
            model_type == "cerebras" and isinstance(cfg.get("custom_endpoint"), dict)
        ):
            header, fmt = _CUSTOM_TYPES.get(model_type, ("Authorization", "Bearer %s"))
            entry = _custom_endpoint_credential(model_key, cfg, header, fmt)
            if entry:
                add_credential(entry)
            else:
                unsupported.append(
                    {
                        "model": model_key,
                        "type": model_type,
                        "reason": "custom endpoint without a $ENV key reference or host",
                    }
                )
            continue

        if model_type in _STATIC_TYPES:
            service, env_var, domain, header, fmt = _STATIC_TYPES[model_type]
            # A custom_endpoint on a static type (e.g. a proxy) wins.
            custom = _custom_endpoint_credential(model_key, cfg, header, fmt)
            add_credential(
                custom
                or {
                    "service": service,
                    "env_var": env_var,
                    "domain": domain,
                    "header": header,
                    "format": fmt,
                    "models": [model_key],
                }
            )
            continue

        unsupported.append(
            {
                "model": model_key,
                "type": model_type or "(missing)",
                "reason": _UNSUPPORTED_TYPES.get(
                    model_type, "no sandbox credential derivation for this type"
                ),
            }
        )

    cred_list = []
    for entry in credentials.values():
        entry = {**entry, "models": sorted(set(entry["models"]))}
        present = providers.credential_present(entry["env_var"])
        if not present and entry["env_var"] == "META_API_KEY":
            meta = providers.get_oauth_provider("meta")
            present = bool(meta and providers.oauth_status(meta)["authenticated"])
        entry["host_credential_present"] = present
        entry["transport"] = "proxy-managed-env"
        cred_list.append(entry)
    cred_list.sort(key=lambda e: (e["service"], e["domain"]))

    from code_puppy.config import get_value

    oauth_list = sorted(oauth.values(), key=lambda e: e["id"])
    if provider:
        cred_list = [c for c in cred_list if c["service"] == provider]
        oauth_list = [o for o in oauth_list if o["id"] == provider]
        egress = {c["domain"] for c in cred_list} | {
            d for o in oauth_list for d in o["domains"]
        }
    return {
        "credentials": cred_list,
        "oauth": oauth_list,
        "egress": sorted(egress),
        "unsupported": unsupported,
        "default_model": (get_value("model") or "").strip() or None,
    }


def render_human(plan: dict[str, Any]) -> list[str]:
    lines = []
    for cred in plan["credentials"]:
        state = "host key present" if cred["host_credential_present"] else "no host key"
        lines.append(
            f"credential {cred['service']}: {cred['env_var']} -> {cred['domain']} "
            f"({state}) for {len(cred['models'])} model(s)"
        )
    for entry in plan["oauth"]:
        lines.append(
            f"oauth {entry['id']}: [{entry['transport']}] egress "
            f"{', '.join(entry['domains'])} for {len(entry['models'])} model(s)"
        )
    for entry in plan["unsupported"]:
        lines.append(
            f"unsupported {entry['model']} [{entry['type']}]: {entry['reason']}"
        )
    if not plan["credentials"] and not plan["oauth"]:
        lines.append("(no sandbox-usable models configured)")
    return lines


def _parse_secret_ls(stdout: str) -> list[str]:
    """Global service-secret names from `sbx secret ls` table output."""
    names = []
    for line in stdout.splitlines():
        cols = line.split()
        if len(cols) >= 3 and cols[0] == "(global)" and cols[1] == "service":
            names.append(cols[2])
    return sorted(set(names))


def _stored_sbx_secrets() -> Optional[list[str]]:
    """Stored global sbx service secrets; None when sbx is unavailable."""
    try:
        result = subprocess.run(
            ["sbx", "secret", "ls"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _parse_secret_ls(result.stdout)


def _remove_sbx_secret(name: str) -> Optional[str]:
    """Remove one global sbx secret; None on success, else an error line."""
    try:
        result = subprocess.run(
            ["sbx", "secret", "rm", name, "-f"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip()[
            :200
        ] or f"exit {result.returncode}"
    return None


def cmd_sbx_plan(out: Out, provider: Optional[str] = None, prune: bool = False) -> int:
    if prune and provider:
        raise SetupError(
            "refusing --prune with --provider: a filtered plan would "
            "delete secrets other providers still need",
            exit_code=2,
        )
    plan = build_plan(provider=provider)
    stored = _stored_sbx_secrets()
    needed = {c["service"] for c in plan["credentials"]}
    stale = (
        [name for name in stored if name not in needed]
        if stored is not None and not provider
        else []
    )
    human = render_human(plan)
    pruned: list[str] = []
    errors: dict[str, str] = {}
    if prune:
        for name in stale:
            error = _remove_sbx_secret(name)
            if error is None:
                pruned.append(name)
                human.append(f"pruned stale sbx secret '{name}'")
            else:
                errors[name] = error
                human.append(f"failed to prune sbx secret '{name}': {error}")
        stale = [name for name in stale if name not in pruned]
    elif stale:
        human.append(
            "stale sbx secrets (not needed by current catalog): "
            + ", ".join(stale)
            + " - re-run with --prune to remove"
        )
    result: dict[str, Any] = {
        **plan,
        "stale_sbx_secrets": stale,
        "pruned_sbx_secrets": pruned,
    }
    if errors:
        result["prune_errors"] = errors
    out.result(result, human=human)
    return 1 if errors else 0


def render_creds_mixin(plan: dict[str, Any]) -> str:
    """Render the v3 credentials-mixin descriptor for a derived plan.

    This descriptor is what makes sbx create the credential env vars
    (e.g. META_API_KEY) inside the sandbox with sentinel values; it
    contains shapes only — never secret values.
    """
    lines = [
        "# syntax=docker/sandbox-kit:3",
        'schemaVersion: "3"',
        "kind: mixin",
        "displayName: Code Puppy credentials (generated)",
        "description: Generated by `code-puppy --setup` from the host model "
        "catalog; regenerated on every export/wizard run. Do not edit.",
        "",
        "capabilities:",
    ]
    if plan["egress"]:
        lines.append("  - type: com.docker.sandbox/network-policy@1")
        lines.append("    config:")
        lines.append("      runtime:")
        lines.append("        allow:")
        lines += [f"          - {domain}:443" for domain in plan["egress"]]
    for cred in plan["credentials"]:
        fmt = cred["format"].replace('"', '\\"')
        lines += [
            "  - type: com.docker.sandbox/credential@1",
            "    optional: true",
            f"    description: {cred['service']} access (derived from host models)",
            "    config:",
            f"      service: {cred['service']}",
            "      phase: runtime",
            "      apiKey:",
            f"        name: {cred['env_var']}",
            "        proxyManaged: true",
            "        inject:",
            f"          - domain: {cred['domain']}",
            f"            header: {cred['header']}",
            f'            format: "{fmt}"',
        ]
    return "\n".join(lines) + "\n"


def write_creds_mixin(
    dest_dir: Any, plan: Optional[dict[str, Any]] = None
) -> Optional[Any]:
    """Write the generated credentials mixin; None if the plan is empty."""
    from pathlib import Path

    if plan is None:
        plan = build_plan()
    if not plan["credentials"] and not plan["egress"]:
        return None
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "code-puppy-creds.yaml").write_text(
        render_creds_mixin(plan), encoding="utf-8"
    )
    (dest / "code-puppy-creds.dockerfile").write_text(
        "# Capability-only mixin: credentials/egress, no files to overlay.\n"
        "FROM scratch\n",
        encoding="utf-8",
    )
    return dest


def cmd_sbx_creds_mixin(out: Out, directory: str, provider: Optional[str]) -> int:
    plan = build_plan(provider=provider)
    dest = write_creds_mixin(directory, plan)
    if dest is None:
        out.result(
            {"written": None, "reason": "plan has no credentials or egress"},
            human="no sandbox-usable models configured; nothing written",
        )
        return 0
    out.result(
        {
            "written": str(dest),
            "services": [c["service"] for c in plan["credentials"]],
            "egress": plan["egress"],
        },
        human=f"credentials mixin written to {dest}",
    )
    return 0
