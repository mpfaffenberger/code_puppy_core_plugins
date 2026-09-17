"""The public install route must never enter the bundled catalog."""

from unittest.mock import MagicMock, patch

import pytest

from code_puppy_core_plugins.mcp_binding_prompt import custom_install


@pytest.fixture
def routes(monkeypatch):
    from code_puppy.command_line.mcp.help_command import HelpCommand
    from code_puppy.command_line.mcp.install_command import InstallCommand
    from code_puppy.command_line.mcp.search_command import SearchCommand
    from code_puppy.command_line.mcp_completion import MCPCompleter

    for cls in (HelpCommand, InstallCommand, SearchCommand):
        monkeypatch.setattr(cls, "execute", cls.execute)
    monkeypatch.setattr(MCPCompleter, "__init__", MCPCompleter.__init__)
    custom_install.install_routes()


@pytest.mark.parametrize("command", ["/mcp install", "/mcp install filesystem"])
@pytest.mark.parametrize("saved", [True, False])
def test_router_opens_custom_form_without_catalog(routes, command, saved):
    from code_puppy.command_line.mcp.handler import MCPCommandHandler

    manager = MagicMock()
    with (
        patch("code_puppy.command_line.mcp.base.get_mcp_manager", return_value=manager),
        patch(
            "code_puppy.command_line.mcp.custom_server_form.run_custom_server_form",
            return_value=saved,
        ) as form,
        patch(
            "code_puppy.command_line.mcp.install_command.run_mcp_install_menu"
        ) as market,
        patch(
            "code_puppy.command_line.mcp.install_command.InstallCommand._install_from_catalog"
        ) as catalog,
    ):
        assert MCPCommandHandler().handle_mcp_command(command)
    form.assert_called_once_with(manager)
    market.assert_not_called()
    catalog.assert_not_called()


def test_help_and_completion_no_longer_advertise_marketplace(routes):
    from code_puppy.command_line.mcp_completion import MCPCompleter

    completer = MCPCompleter()
    assert "search" not in completer.all_subcommands
    assert "JSON" in completer.all_subcommands["install"]
    with patch.object(custom_install, "emit_info") as output:
        custom_install.custom_help(None, [])
    assert "/mcp search" not in output.call_args.args[0]
    assert "custom JSON" in output.call_args.args[0]


def test_form_failure_is_reported():
    with (
        patch(
            "code_puppy.command_line.mcp.custom_server_form.run_custom_server_form",
            side_effect=ValueError("broken"),
        ),
        patch.object(custom_install, "emit_error") as output,
    ):
        custom_install.install_custom(MagicMock(), [], group_id="group")
    assert "broken" in output.call_args.args[0]
    assert output.call_args.kwargs["message_group"] == "group"
