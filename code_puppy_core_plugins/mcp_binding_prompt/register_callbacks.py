"""Bridge synchronous installers to the existing binding menu via startup."""

import logging

from code_puppy.callbacks import register_callback

logger = logging.getLogger(__name__)


def prompt_bind_after_install_sync(server_name: str) -> None:
    """Run in the installer's foreground, never schedule a competing UI task."""
    from code_puppy.agents import get_available_agents
    from code_puppy.command_line.mcp_binding_menu import (
        build_post_install_menu,
        menu_session,
        set_awaiting_user_input,
    )

    agents = sorted(get_available_agents(), key=str.lower)
    if not agents:
        return
    set_awaiting_user_input(True)
    try:
        with menu_session():
            build_post_install_menu(server_name, agents, alt_screen=False).run()
    finally:
        set_awaiting_user_input(False)


def _install() -> None:
    try:
        from code_puppy.command_line import mcp_binding_menu

        if not hasattr(mcp_binding_menu, "prompt_bind_after_install_sync"):
            mcp_binding_menu.prompt_bind_after_install_sync = (
                prompt_bind_after_install_sync
            )
        from .custom_install import install_routes

        install_routes()
        from .http_form import install_http_form

        install_http_form()
    except Exception:
        logger.exception("Could not install MCP binding prompt compatibility bridge")


register_callback("startup", _install)
