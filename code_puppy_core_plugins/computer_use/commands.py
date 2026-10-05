"""Slash commands for explicit computer-use authorization."""

from __future__ import annotations

import shlex

from code_puppy.i18n import t
from code_puppy.messaging import emit_error, emit_info, emit_success, emit_warning

from .backend_types import ComputerUseError
from .policy import policy_store


def command_help() -> list[tuple[str, str]]:
    return [
        (
            "computer-use",
            t("plugin.computer-use.command.help"),
        )
    ]


def handle_command(command: str, name: str) -> bool | None:
    try:
        return _handle_command(command, name)
    except (ComputerUseError, OSError) as exc:
        emit_error(str(exc))
        return True


def _handle_command(command: str, name: str) -> bool | None:
    if name != "computer-use":
        return None
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        emit_error(f"Invalid /computer-use command: {exc}")
        return True
    subcommand = tokens[1].casefold() if len(tokens) > 1 else "status"
    if subcommand == "status":
        emit_info(f"Computer Use policy: {policy_store.status()}")
    elif subcommand in {"enable", "disable"}:
        enabled = subcommand == "enable"
        policy_store.set_enabled(enabled)
        if enabled:
            emit_success(
                "Computer Use enabled. It can now view and control allowed apps."
            )
        else:
            emit_warning("Computer Use disabled in settings.")
    elif subcommand == "pause":
        policy_store.set_paused(True)
        emit_warning("Computer Use paused immediately.")
    elif subcommand == "resume":
        policy_store.set_paused(False)
        emit_success("Computer Use resumed.")
    elif subcommand in {"allow", "deny"}:
        if len(tokens) < 3:
            emit_error(t("plugin.computer-use.command.policy_usage"))
            return True
        bundle_id = tokens[2]
        if subcommand == "deny":
            policy_store.deny(bundle_id)
            emit_warning(f"Computer Use denied for {bundle_id}.")
        else:
            policy_store.allow(bundle_id)
            emit_success(f"Computer Use allowed for {bundle_id}.")
    else:
        emit_error(
            "Usage: /computer-use [status|enable|disable|pause|resume|allow|deny]"
        )
    return True
