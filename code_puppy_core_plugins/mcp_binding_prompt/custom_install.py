"""Custom JSON replaces the bundled MCP marketplace at the command boundary."""

import logging
from functools import wraps

from code_puppy.i18n import t
from code_puppy.messaging import emit_error, emit_info

logger = logging.getLogger(__name__)


def install_custom(command, args, group_id=None):
    """Keep the existing form's save, cancel, and binding semantics."""
    from code_puppy.command_line.mcp.custom_server_form import run_custom_server_form

    try:
        run_custom_server_form(command.manager)
    except (KeyboardInterrupt, EOFError):
        return
    except Exception as exc:
        logger.exception("Custom MCP installation failed")
        emit_error(
            t("mcp.custom_install.failed", error=str(exc)), message_group=group_id
        )


def marketplace_removed(command, args, group_id=None):
    emit_info(t("mcp.custom_install.no_marketplace"), message_group=group_id)


def custom_help(command, args, group_id=None):
    emit_info(t("mcp.custom_install.help"), message_group=group_id)


def install_routes():
    # Known slash commands do not reach custom_command. Install the override
    # at startup, leaving core command_line files and existing instances intact.
    from code_puppy.command_line.mcp.install_command import InstallCommand
    from code_puppy.command_line.mcp.search_command import SearchCommand
    from code_puppy.command_line.mcp.help_command import HelpCommand
    from code_puppy.command_line.mcp_completion import MCPCompleter

    original = MCPCompleter.__init__
    if getattr(original, "_custom_json_install", False):
        return  # already routed -- never double-wrap the completer
    InstallCommand.execute = install_custom
    SearchCommand.execute = marketplace_removed
    HelpCommand.execute = custom_help

    @wraps(original)
    def initialize(completer, *args, **kwargs):
        original(completer, *args, **kwargs)
        for commands in (completer.general_subcommands, completer.all_subcommands):
            commands.pop("search", None)
            commands["install"] = t("mcp.custom_install.description")

    initialize._custom_json_install = True
    MCPCompleter.__init__ = initialize
