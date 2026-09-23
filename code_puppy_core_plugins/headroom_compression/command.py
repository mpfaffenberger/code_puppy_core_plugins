"""``/headroom`` command -- enable, disable, restart, status, plus a small
allowlisted passthrough for read-only diagnostics.

Enable/disable/restart/status are handled directly (they touch this plugin's
own config + proxy lifecycle). A deliberately small, explicit allowlist of
headroom's own read-only diagnostic subcommands (doctor/savings/
output-savings/perf/dashboard) is forwarded to the real ``headroom`` binary
so users get the same verification workflow they'd use standalone, without
dropping to a raw shell.

This is intentionally NOT a blanket passthrough. headroom's CLI also ships
subcommands that would be actively harmful to expose here uncurated: ones
that start their own untracked long-running proxy/server
(``proxy``, ``mcp``, ``deploy`` -- would hang the session or desync from
this plugin's own proxy lifecycle), ones that mutate *other* tools' configs
(``wrap``/``unwrap``/``install``/``init`` -- for Claude Code/Cursor/Codex,
not us), one that mutates the headroom binary itself mid-session
(``update``), and several that are simply moot here because this plugin
always runs headroom with ``--stateless`` (``memory``, ``inspect`` read a
disk-backed history that ``--stateless`` disables). Anything not on the
allowlist is rejected with a warning rather than silently forwarded --
never pass an arbitrary user-typed subcommand straight into
``subprocess.run``.

Adding a new command to the allowlist should be a deliberate, reviewed
decision, not a default.
"""

from __future__ import annotations

import subprocess
from typing import Optional

from code_puppy.messaging import emit_error, emit_info, emit_success, emit_warning

from . import config, proxy

# Read-only headroom CLI diagnostics known to be useful for verifying/
# debugging headroom-in-code-puppy. See the module docstring for why the
# rest of headroom's CLI surface is deliberately excluded.
_PASSTHROUGH_ALLOWLIST = frozenset(
    {"doctor", "savings", "output-savings", "perf", "dashboard"}
)


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

    if subcommand in _PASSTHROUGH_ALLOWLIST:
        _run_passthrough([subcommand] + parts[2:])
        return True

    emit_warning(
        f"'{subcommand}' isn't a supported /headroom subcommand.\n"
        f"  Built-in: enable <url> | disable | restart | status\n"
        f"  Diagnostics: {', '.join(sorted(_PASSTHROUGH_ALLOWLIST))}\n"
        "  Anything else: run `headroom <subcommand>` directly in a shell."
    )
    return True


def _run_passthrough(args: list) -> None:
    """Run an allowlisted ``headroom <args>`` and stream output to the terminal.

    No timeout is applied -- ``dashboard`` opens a browser and exits on its
    own, and log/history sizes for ``perf``/``savings`` are unbounded; a
    fixed timeout would kill those mid-flight. Safe specifically because
    every caller of this function has already been checked against
    ``_PASSTHROUGH_ALLOWLIST`` -- never call it with an unvalidated arg.
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
            "restart | doctor | savings | output-savings | perf | dashboard",
        )
    ]
