"""``/headroom`` command -- enable, disable, restart, status, plus a small
allowlisted passthrough for read-only diagnostics.

Enable/disable/restart/status are handled directly (they touch this plugin's
own config + proxy lifecycle). A deliberately small, explicit allowlist of
headroom's own read-only diagnostic subcommands (doctor/savings/
output-savings/perf/dashboard) is forwarded to the real ``headroom`` binary
so users get the same verification workflow they'd use standalone, without
dropping to a raw shell.

The bar for this allowlist is two-part, and BOTH parts are required:
  1. Safe -- individually verified against ``headroom <cmd> --help`` to have
     no destructive or state-mutating flag reachable through forwarded args.
  2. Actually useful *inside a code-puppy session specifically* -- something
     a user would reasonably want without breaking their coding flow to
     drop to a shell ("is my routing/savings working right now"). Safety
     alone is NOT sufficient: this list is intentionally not exhaustive,
     and does not grow just because a command turns out to be harmless.
     Anything safe-but-niche (e.g. headroom's own internal feature-rollout
     inspection, or its telemetry-disclosure command) is left off on
     purpose -- those are one-off audit actions a user can run directly via
     ``headroom <cmd>`` in a terminal, not something that benefits from
     living inside a coding session.

This is intentionally NOT a blanket passthrough. headroom's CLI also ships
subcommands that would be actively harmful to expose here uncurated: ones
that start their own untracked long-running proxy/server
(``proxy``, ``mcp``, ``deploy`` -- would hang the session or desync from
this plugin's own proxy lifecycle), ones that mutate *other* tools' configs
(``wrap``/``unwrap``/``install``/``init`` -- for Claude Code/Cursor/Codex,
not us), one that mutates the headroom binary itself mid-session
(``update``), ones that make real LLM calls or run long benchmark suites
(``learn`` -- also has a ``--apply`` flag that writes to project files;
``evals``), one that handles privacy-sensitive raw MITM-captured traffic
(``capture``), one that only audits *other* tools' transcript formats
(``audit-reads`` -- Claude Code/Codex, not code-puppy's own), one that
merges/recovers on-disk state (``recover``), several that are simply moot
here because this plugin always runs headroom with ``--stateless``
(``memory`` -- disk-backed history is disabled; ``inspect`` -- its own
``--help`` states it requires the proxy to be started with
``--log-messages``/``--log-file``, which this plugin never does), and two
that are perfectly safe but fail the in-session-utility bar rather than the
safety bar (``telemetry``, ``rollout`` -- one-off audit/debug actions, not
something a coding session benefits from). Anything not on the allowlist is
rejected with a warning rather than silently forwarded -- never pass an
arbitrary user-typed subcommand straight into ``subprocess.run``.

Adding a new command to the allowlist should be a deliberate, reviewed
decision verified against both criteria above, not a default.
"""

from __future__ import annotations

import subprocess
from typing import Optional

from code_puppy.messaging import emit_error, emit_info, emit_success, emit_warning

from . import config, proxy

# Read-only headroom CLI diagnostics known to be both safe AND genuinely
# useful to run without leaving a code-puppy session. See the module
# docstring for why the rest of headroom's CLI surface -- including some
# individually-safe commands -- is deliberately excluded.
_PASSTHROUGH_ALLOWLIST = frozenset(
    {"doctor", "savings", "output-savings", "perf", "dashboard"}
)

# Every other real headroom subcommand as of headroom-ai 0.38.0, deliberately
# reviewed and rejected -- either for safety (see module docstring) or
# because it fails the in-session-utility bar despite being safe (telemetry,
# rollout). This set exists so the drift helpers below can tell "genuinely
# new, never seen" apart from "seen and deliberately excluded" -- without
# it, a drift check would re-flag the same 23 known-excluded names forever
# instead of only the ones that actually need a human decision.
_KNOWN_EXCLUDED = frozenset(
    {
        "agent-savings", "audit-reads", "capture", "copilot-auth", "deploy",
        "diff", "evals", "init", "inspect", "install", "learn", "loc", "mcp",
        "memory", "proxy", "recover", "rollout", "sg", "telemetry", "tools",
        "unwrap", "update", "wrap",
    }
)


def _discover_live_headroom_subcommands(bin_path: str) -> Optional[frozenset]:
    """Best-effort parse of ``headroom --help``'s ``Commands:`` section.

    Returns None (never raises) if headroom can't be run or its help output
    doesn't look like the Click format we expect -- callers should treat
    that as "couldn't check," not "no subcommands exist."
    """
    try:
        result = subprocess.run(
            [bin_path, "--help"], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    try:
        start = lines.index("Commands:")
    except ValueError:
        return None
    names = set()
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        if not line.startswith("  "):
            break
        names.add(line.strip().split()[0])
    return frozenset(names) if names else None


def find_unreviewed_headroom_subcommands(bin_path: str) -> Optional[frozenset]:
    """Subcommands headroom now ships that we've never classified as
    allow/exclude. None means discovery failed (headroom missing or its
    --help format changed) rather than "nothing new" -- don't conflate the
    two in a caller.
    """
    live = _discover_live_headroom_subcommands(bin_path)
    if live is None:
        return None
    return live - _PASSTHROUGH_ALLOWLIST - _KNOWN_EXCLUDED


def find_stale_allowlist_entries(bin_path: str) -> Optional[frozenset]:
    """Allowlisted subcommands that no longer exist in the live headroom
    CLI (renamed or removed upstream). None means discovery failed.
    """
    live = _discover_live_headroom_subcommands(bin_path)
    if live is None:
        return None
    return _PASSTHROUGH_ALLOWLIST - live


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
