"""``setup doctor``: read-only health checks over the current setup."""

from __future__ import annotations

import os
from typing import Any

from . import providers


def _check(name: str, status: str, detail: str, hint: str = "") -> dict[str, Any]:
    entry = {"check": name, "status": status, "detail": detail}
    if hint:
        entry["hint"] = hint
    return entry


def run_doctor(check_network: bool = False) -> tuple[dict[str, Any], int]:
    """Run all checks; return (report, exit_code). Never writes anything."""
    checks: list[dict[str, Any]] = []
    from code_puppy import config as cp_config

    # Identity / first-run state.
    missing = [
        key
        for key in ("puppy_name", "owner_name")
        if not (cp_config.get_value(key) or "").strip()
    ]
    if missing:
        checks.append(
            _check(
                "identity",
                "fail",
                f"puppy.cfg is missing required keys: {', '.join(missing)}",
                "Run `code-puppy --setup wizard` or `config set` for each key.",
            )
        )
    else:
        checks.append(_check("identity", "ok", "puppy.cfg identity keys are set"))

    # Directories.
    for label, path in (
        ("config dir", cp_config.CONFIG_DIR),
        ("data dir", cp_config.DATA_DIR),
    ):
        if os.path.isdir(path) and os.access(path, os.W_OK):
            checks.append(_check(label, "ok", path))
        elif os.path.isdir(path):
            checks.append(_check(label, "warn", f"{path} exists but is not writable"))
        else:
            checks.append(_check(label, "warn", f"{path} does not exist yet"))

    # Model catalog + default model.
    catalog: dict[str, Any] = {}
    try:
        from code_puppy.model_factory import ModelFactory

        catalog = ModelFactory.load_config()
    except Exception as exc:
        checks.append(_check("model catalog", "fail", f"failed to load: {exc}"))
    if catalog:
        checks.append(
            _check("model catalog", "ok", f"{len(catalog)} model(s) configured")
        )
    elif "model catalog" not in {c["check"] for c in checks}:
        checks.append(
            _check(
                "model catalog",
                "fail",
                "no models configured",
                "Authenticate a provider (`setup auth login`) or add one "
                "with `setup model add`.",
            )
        )
    default_model = (cp_config.get_value("model") or "").strip()
    if catalog:
        if default_model and default_model in catalog:
            checks.append(_check("default model", "ok", default_model))
        elif default_model:
            checks.append(
                _check(
                    "default model",
                    "fail",
                    f"'{default_model}' is not in the configured catalog",
                    "Pick one with `setup model default <key>`.",
                )
            )
        else:
            checks.append(
                _check(
                    "default model",
                    "warn",
                    "no default model set; Code Puppy will auto-select",
                    "Pin one with `setup model default <key>`.",
                )
            )

    # MCP config parse.
    mcp_path = cp_config.MCP_SERVERS_FILE
    if not os.path.exists(mcp_path):
        checks.append(_check("mcp servers", "ok", "none configured"))
    else:
        try:
            with open(mcp_path, encoding="utf-8") as handle:
                servers = cp_config._parse_mcp_servers_mapping(handle.read())
            checks.append(
                _check("mcp servers", "ok", f"{len(servers)} server(s) configured")
            )
        except Exception as exc:
            checks.append(_check("mcp servers", "fail", f"{mcp_path}: {exc}"))

    # Plugins.
    try:
        from code_puppy.plugins import get_loaded_plugins
        from code_puppy.plugins.config import get_disabled_plugins

        loaded = get_loaded_plugins()
        total = sum(len(names) for names in loaded.values())
        checks.append(
            _check(
                "plugins",
                "ok",
                f"{total} loaded, {len(get_disabled_plugins())} disabled",
            )
        )
    except Exception as exc:
        checks.append(_check("plugins", "warn", f"could not introspect: {exc}"))

    # Provider authentication.
    status = providers.all_provider_status()
    authed_oauth = [s["id"] for s in status.oauth if s["authenticated"]]
    authed_keys = [s["env_var"] for s in status.api_keys if s["authenticated"]]
    if authed_oauth or authed_keys:
        checks.append(
            _check(
                "authentication",
                "ok",
                f"oauth: {authed_oauth or 'none'}; api keys: {authed_keys or 'none'}",
            )
        )
    else:
        checks.append(
            _check(
                "authentication",
                "fail",
                "no provider is authenticated",
                "Run `code-puppy --setup auth login <provider>`.",
            )
        )

    if check_network:
        checks.extend(_network_checks(status))

    worst = 1 if any(c["status"] == "fail" for c in checks) else 0
    report = {
        "ok": worst == 0,
        "checks": checks,
        "providers": {"oauth": status.oauth, "api_keys": status.api_keys},
    }
    return report, worst


def _network_checks(status: providers.ProviderStatus) -> list[dict[str, Any]]:
    import urllib.request

    domains = {"models.dev"}
    for entry in status.oauth:
        if entry["authenticated"]:
            domains.update(entry.get("api_domains") or [])
    results = []
    for domain in sorted(domains):
        try:
            request = urllib.request.Request(
                f"https://{domain}/",
                method="HEAD",
                headers={"User-Agent": "code-puppy-setup"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                code = response.status
            results.append(_check(f"network {domain}", "ok", f"HTTP {code}"))
        except Exception as exc:
            # A 4xx still proves reachability; only transport errors fail.
            code = getattr(exc, "code", None)
            if code:
                results.append(_check(f"network {domain}", "ok", f"HTTP {code}"))
            else:
                results.append(_check(f"network {domain}", "fail", str(exc)))
    return results


def render_human(report: dict[str, Any]) -> list[str]:
    icons = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
    lines = []
    for entry in report["checks"]:
        lines.append(f"[{icons[entry['status']]}] {entry['check']}: {entry['detail']}")
        if entry.get("hint"):
            lines.append(f"       -> {entry['hint']}")
    lines.append("doctor: healthy" if report["ok"] else "doctor: problems found")
    return lines
