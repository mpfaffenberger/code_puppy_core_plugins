"""Entry point: wires headroom_compression's callbacks into core."""

from __future__ import annotations

import atexit

from code_puppy.callbacks import register_callback

from . import config, proxy
from .command import get_headroom_command_help, handle_headroom_command


async def _start_if_enabled() -> None:
    if not config.is_enabled():
        return
    url = config.get_upstream_url()
    if not url:
        return
    proxy.start_proxy(url)


register_callback("startup", _start_if_enabled)
register_callback("custom_command_help", get_headroom_command_help)
register_callback("custom_command", handle_headroom_command)
register_callback("resolve_custom_endpoint_url", proxy.resolve_custom_endpoint_url)
register_callback("agent_exception", proxy.on_agent_exception_check_proxy)

# Belt-and-braces, matching the herdr plugin's pattern: atexit doesn't run on
# SIGTERM/SIGHUP, and an orphaned proxy left holding the port is exactly the
# precondition for the stale-adoption hazard documented in proxy.start_proxy.
register_callback("shutdown", proxy.stop_proxy)
atexit.register(proxy.stop_proxy)
