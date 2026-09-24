"""Entry point: wires headroom_compression's callbacks into core."""

from __future__ import annotations

import atexit
import logging

from code_puppy.callbacks import register_callback as _core_register_callback

from . import config, proxy
from .command import get_headroom_command_help, handle_headroom_command

logger = logging.getLogger(__name__)


async def _start_if_enabled() -> None:
    if not config.is_enabled():
        return
    url = config.get_upstream_url()
    if not url:
        return
    proxy.start_proxy(url)


def _register_all(register_callback=_core_register_callback) -> bool:
    """Register every callback IF the core `resolve_custom_endpoint_url`
    hook is available; register NOTHING at all otherwise.

    This plugin is useless without that hook -- it's the only thing that
    actually routes traffic through the proxy. So it's registered first, in
    isolation: on an older code-puppy that doesn't have it yet,
    `register_callback` raises ValueError, and we must not have already
    registered `startup` (which would spawn a proxy subprocess with nothing
    to ever route to it or clean it up -- an orphaned process leak) or the
    `/headroom` command (which would look functional but silently do
    nothing). Registering nothing at all is the correct degraded state
    until core catches up.

    `register_callback` is injectable so the degraded-hook path is directly
    unit-testable without reimporting this module (which would otherwise
    accumulate duplicate registrations in the real global callback registry
    across test runs).

    Returns True if the plugin ended up active, False if it loaded inactive.
    """
    try:
        register_callback("resolve_custom_endpoint_url", proxy.resolve_custom_endpoint_url)
    except ValueError:
        logger.warning(
            "headroom_compression: this code-puppy build doesn't support the "
            "resolve_custom_endpoint_url hook yet -- plugin loaded but inactive "
            "(no proxy, no /headroom command). Upgrade code-puppy to use it."
        )
        return False

    register_callback("startup", _start_if_enabled)
    register_callback("custom_command_help", get_headroom_command_help)
    register_callback("custom_command", handle_headroom_command)
    register_callback("agent_exception", proxy.on_agent_exception_check_proxy)

    # Belt-and-braces, matching the herdr plugin's pattern: atexit doesn't
    # run on SIGTERM/SIGHUP, and an orphaned proxy left holding the port is
    # exactly the precondition for the stale-adoption hazard documented in
    # proxy.start_proxy / proxy._is_proxy_healthy.
    register_callback("shutdown", proxy.stop_proxy)
    atexit.register(proxy.stop_proxy)
    return True


_ACTIVE = _register_all()
