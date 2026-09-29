"""The synchronous installation path must actually display agent choices."""

from unittest.mock import MagicMock, patch

from code_puppy_core_plugins.mcp_binding_prompt import register_callbacks as plugin


def test_startup_restores_missing_sync_entrypoint(monkeypatch):
    from code_puppy.command_line import mcp_binding_menu

    monkeypatch.delattr(
        mcp_binding_menu, "prompt_bind_after_install_sync", raising=False
    )
    with (
        patch("code_puppy_core_plugins.mcp_binding_prompt.http_form.install_http_form"),
        patch(
            "code_puppy_core_plugins.mcp_binding_prompt.custom_install.install_routes"
        ) as routes,
    ):
        plugin._install()
    routes.assert_called_once()
    assert (
        mcp_binding_menu.prompt_bind_after_install_sync
        is plugin.prompt_bind_after_install_sync
    )
    monkeypatch.delattr(mcp_binding_menu, "prompt_bind_after_install_sync")


def test_prompt_runs_inline_and_restores_input_flag():
    from code_puppy.command_line import mcp_binding_menu

    menu = MagicMock()
    with (
        patch(
            "code_puppy.agents.get_available_agents",
            return_value={"zebra": {}, "puppy": {}},
        ),
        patch.object(
            mcp_binding_menu, "build_post_install_menu", return_value=menu
        ) as build,
        patch.object(mcp_binding_menu, "menu_session"),
        patch.object(mcp_binding_menu, "set_awaiting_user_input") as waiting,
    ):
        plugin.prompt_bind_after_install_sync("remote")
    build.assert_called_once_with("remote", ["puppy", "zebra"], alt_screen=False)
    menu.run.assert_called_once()
    assert [call.args[0] for call in waiting.call_args_list] == [True, False]
