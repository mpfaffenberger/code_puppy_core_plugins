"""``/headroom`` command -- enable, disable, restart, status, plus passthrough.

Enable/disable/restart/status are handled directly (they touch this plugin's
own config + proxy lifecycle). Everything else (``doctor``, ``perf``,
``dashboard``, ``savings``, ``memory list``, ... any current or future
headroom CLI subcommand) is forwarded verbatim to the real ``headroom``
binary -- no code changes needed here when headroom adds new commands, and
users keep their existing muscle memory (e.g. ``headroom doctor`` to verify
routing) without having to drop out to a raw shell.
"""

from __future__ import annotations

import subprocess
from typing import Optional

from code_puppy.messaging import emit_error, emit_info, emit_success, emit_warning

from . import config, proxy


def handle_headroom_command(command: str, name: str) -> Optional[bool]:
    if name != "headroom":
        return None

    parts = command.strip().split()
    subcommand = parts[1].lower() if len(parts) > 1 else "status"

    if subcommand == "enable":
        if len(parts) < 3:
            emit_error("Usage: /headroom enable <upstream-anthropic-url>")
            return True
        url = parts[2]
        if not config.enable(url):
            emit_error(f"Not a valid http(s) URL: {url}")
            return True
        if proxy.start_proxy(url):
            emit_success(f"headroom proxy active, routing {url} through it.")
        else:
            emit_warning(
                "headroom_compression is enabled, but the proxy did not start.\n"
                "  Is the `headroom` binary installed? (pip install headroom-ai)"
            )
        return True

    if subcommand == "disable":
        config.disable()
        proxy.stop_proxy()
        emit_info("headroom compression disabled; requests go direct.")
        return True

    if subcommand == "restart":
        if proxy.restart_proxy():
            emit_success("headroom proxy restarted.")
        else:
            emit_error("Failed to restart headroom proxy.")
        return True

    if subcommand == "status":
        _show_status()
        return True

    _run_passthrough([subcommand] + parts[2:])
    return True


def _run_passthrough(args: list) -> None:
    """Run ``headroom <args>`` and stream its output straight to the terminal.

    No timeout is applied -- some headroom subcommands are long-running or
    open a browser and exit on their own (``dashboard``), or stream results
    of unknown size (``memory list``); a fixed timeout would kill those
    mid-flight.
    """
    bin_path = proxy._headroom_bin()
    if not bin_path:
        emit_warning(
            "headroom is not installed -- install it yourself "
            "(pip install headroom-ai) then retry."
        )
        return
    try:
        subprocess.run([bin_path] + args)
    except Exception as exc:
        emit_error(f"headroom error: {exc}")


def _show_status() -> None:
    enabled = config.is_enabled()
    url = config.get_upstream_url()
    active = proxy.is_active()
    emit_info(f"headroom_compression: {'enabled' if enabled else 'disabled'}")
    emit_info(f"  upstream: {url or '(not set)'}")
    emit_info(f"  proxy: {'active' if active else 'inactive'}")


def get_headroom_command_help() -> list:
    return [
        (
            "headroom",
            "Route a custom Anthropic endpoint through a local headroom "
            "compression proxy -- /headroom enable <url> | disable | status | "
            "restart | <any headroom CLI subcommand, e.g. doctor, perf, savings>",
        )
    ]
