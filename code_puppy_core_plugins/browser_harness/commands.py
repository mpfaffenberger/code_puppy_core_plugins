"""/browser - connect Code Puppy to a real browser through browser-harness."""

from __future__ import annotations

import shlex

from code_puppy.messaging import emit_error, emit_info, emit_success, emit_warning

from . import browser, cli
from . import policy
from .policy import BrowserHarnessError

USAGE = (
    "Usage: /browser [status|enable|disable|doctor|connect <devtools-url>|"
    "disconnect|install|recordings [on|off]]"
)


def command_help() -> list[tuple[str, str]]:
    return [
        (
            "browser",
            "Connect, enable, or diagnose browser-harness browser control",
        )
    ]


def handle_command(command: str, name: str) -> bool | None:
    if name != "browser":
        return None
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        emit_error(f"Invalid /browser command: {exc}")
        return True
    subcommand = tokens[1].casefold() if len(tokens) > 1 else "status"

    if subcommand == "status":
        _emit_status()
    elif subcommand == "doctor":
        _emit_doctor()
    elif subcommand == "install":
        _emit_install_help()
    elif subcommand == "enable":
        policy.settings_store.set_enabled(True)
        _refresh_tool_registry()
        emit_success(
            "Browser control enabled. Code Puppy can now drive your real, "
            "signed-in browser; the browser tools are registered for this session."
        )
    elif subcommand == "disable":
        policy.settings_store.set_enabled(False)
        emit_warning("Browser control disabled; running browser tools is blocked.")
    elif subcommand == "connect":
        _connect(tokens[2] if len(tokens) > 2 else None)
    elif subcommand == "disconnect":
        policy.settings_store.clear_endpoint()
        emit_success(
            "Cleared the saved endpoint. The harness will auto-discover your "
            "running Chromium browser again."
        )
    elif subcommand == "recordings":
        _recordings(tokens[2] if len(tokens) > 2 else None)
    else:
        emit_error(USAGE)
    return True


def _refresh_tool_registry() -> None:
    """Merge the newly consented tools in without asking the user to restart.

    Asking for the available tool names re-runs Code Puppy's ``register_tools``
    hook, which is exactly how plugin tools reach ``TOOL_REGISTRY``. Disabling
    needs no such dance: the tools refuse to run on their own and vanish from
    the registry on the next start.
    """
    try:
        from code_puppy.tools import get_available_tool_names

        get_available_tool_names()
    except Exception:  # pragma: no cover - only costs a restart, not a session
        pass


def _emit_status() -> None:
    _emit_install_state()
    consent = policy.settings_store.consent_state()
    if consent == "unset":
        emit_warning(
            "Browser control is awaiting your one-time consent. Run `/browser "
            "enable` to allow it, or `/browser disable` to keep it off."
        )
    else:
        emit_info(f"Browser control consent: {consent}")
    _emit_connection()
    _emit_browsers()


def _emit_install_state() -> None:
    try:
        path = cli.executable()
    except BrowserHarnessError as exc:
        emit_error(str(exc))
        return
    if path is None:
        emit_warning(
            f"browser-harness is not installed. Install it with:\n  {cli.INSTALL_COMMAND}"
        )
        return
    emit_info(f"browser-harness {cli.version() or 'unknown version'} ({path})")


def _emit_connection() -> None:
    ambient, saved = cli.ambient_endpoint(), policy.settings_store.endpoint()
    if ambient:
        emit_info(f"CDP endpoint: {ambient} (from BU_CDP_WS/BU_CDP_URL)")
    elif saved:
        emit_info(f"CDP endpoint: {saved} (saved by /browser connect)")
    else:
        emit_info(
            "CDP endpoint: auto-discovery - the harness attaches to whichever "
            "Chromium browser is running"
        )
    endpoints = browser.reachable_endpoints(
        [ambient or saved] if ambient or saved else []
    )
    live = [endpoint for endpoint in endpoints if endpoint.reachable]
    for endpoint in live:
        emit_info(f"  answering on {endpoint.url}: {endpoint.product}")
    if not live:
        emit_info("  no local DevTools endpoint is answering yet")


def _emit_browsers() -> None:
    installed = browser.detect_browsers()
    drivable = browser.drivable_browsers(installed)
    if not drivable:
        emit_warning(f"No drivable browser is installed. {browser.UNSUPPORTED_NOTE}")
        _emit_install_help()
    for item in drivable:
        emit_info(f"  {item.name}: {'running' if item.running else 'installed'}")
    blocked = [item.name for item in browser.undrivable_browsers(installed)]
    if blocked:
        emit_info(f"  present but not drivable: {', '.join(blocked)}")


def _emit_install_help() -> None:
    emit_info(
        "Install a Chromium-family browser, then ask me to retry:\n  "
        + "\n  ".join(browser.install_suggestions())
    )


def _connect(target: str | None) -> None:
    if not target:
        emit_error(
            "Usage: /browser connect <devtools-url>  e.g. "
            "http://127.0.0.1:9222 or wss://your-browser.example/cdp"
        )
        return
    try:
        policy.settings_store.set_endpoint(target)
    except BrowserHarnessError as exc:
        emit_error(str(exc))
        return
    endpoint = browser.probe_endpoint(target)
    if endpoint.reachable:
        emit_success(f"Saved {endpoint.url} - {endpoint.product}")
    elif endpoint.reachable is None:
        emit_success(
            f"Saved {endpoint.url}. The harness resolves WebSocket endpoints on "
            "first use, so it is not probed here."
        )
    else:
        emit_warning(
            f"Saved {endpoint.url}, but nothing answered just now. Start that "
            "browser, or run `/browser doctor`."
        )


def _emit_doctor() -> None:
    try:
        result = cli.run_command(["--doctor"], timeout=60.0)
    except BrowserHarnessError as exc:
        emit_error(str(exc))
        return
    emit_info(result.stdout.strip() or "(no report)")
    if not result.ok:
        emit_warning(result.failure())
        return
    emit_success("browser-harness reports a healthy connection.")


def _recordings(action: str | None) -> None:
    args = {
        "on": ["recordings", "enable"],
        "off": ["recordings", "disable"],
        None: ["recordings"],
    }.get(action.casefold() if action else None)
    if args is None:
        emit_error("Usage: /browser recordings [on|off]")
        return
    try:
        result = cli.run_command(args, timeout=30.0)
    except BrowserHarnessError as exc:
        emit_error(str(exc))
        return
    emit_info(result.stdout.strip() if result.ok else result.failure())
