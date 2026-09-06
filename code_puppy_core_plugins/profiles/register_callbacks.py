"""CLI and slash-command entry points for configuration profiles."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from code_puppy.callbacks import register_callback
from code_puppy.i18n import add_catalog_dir, t

from . import config

add_catalog_dir(Path(__file__).parent / "locales")


def register_cli_args(parser):
    parser.add_argument(
        "--profile", metavar="NAME", default="default", help=t("profiles.cli_help")
    )


def handle_cli_args(args):
    try:
        config.activate_profile(getattr(args, "profile", "default"))
    except (OSError, ValueError) as exc:
        print(t("profiles.error", detail=str(exc)), file=sys.stderr)
        return {"handled": True, "exit_code": 2}
    return None


def switch_profile(name):
    """Rebuild the current agent so cached model settings cannot leak."""
    from code_puppy.agents.agent_manager import (
        get_current_agent_name,
        set_current_agent,
    )

    previous = config.active_profile()
    agent = get_current_agent_name()
    config.activate_profile(name)
    try:
        set_current_agent(agent)
    except Exception:
        config.activate_profile(previous)
        raise


def custom_command(command, name):
    if name != "profile":
        return None
    from code_puppy.messaging import emit_error, emit_info

    try:
        parts = shlex.split(command)[1:]
        if not parts:
            from .menu import run_menu

            run_menu()
        elif parts == ["list"]:
            emit_info(
                t(
                    "profiles.list",
                    active=config.active_profile(),
                    names=", ".join(config.list_profiles()),
                )
            )
        elif len(parts) == 2 and parts[0] == "create":
            config.create_profile(parts[1])
            emit_info(t("profiles.created", name=parts[1]))
        elif len(parts) == 2 and parts[0] == "use":
            switch_profile(parts[1])
            emit_info(t("profiles.active", name=parts[1]))
        else:
            emit_info(t("profiles.usage"))
    except (OSError, ValueError, RuntimeError) as exc:
        emit_error(t("profiles.error", detail=str(exc)))
    return True


def custom_help():
    return [("profile", t("profiles.help"))]


register_callback("register_cli_args", register_cli_args)
register_callback("handle_cli_args", handle_cli_args)
register_callback("custom_command", custom_command)
register_callback("custom_command_help", custom_help)
