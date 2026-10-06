"""Change an ACP session's route: the agent and model it runs on.

Route changes arrive as ``session/set_config_option`` (the ``agent`` and
``model`` options) or as a ``/agent <name>`` / ``/model <id>`` prompt. Either
way the session's agent is rebuilt on the new route with its history, the
route is saved, and only then reported -- in the response and as a
``config_option_update``. Nothing here touches the terminal's global model or
agent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from acp.schema import ConfigOptionUpdate

from code_puppy_core_plugins.acp import content, route_runtime, session_config, state
from code_puppy_core_plugins.acp.route import expected_epoch
from code_puppy_core_plugins.acp.session import ACPSession

logger = logging.getLogger(__name__)


def route_command(prompt: List[Any]) -> Optional[tuple[str, str]]:
    """Map a ``/model <id>`` or ``/agent <name>`` prompt to a route change.

    Run through the terminal command handler these would change the
    process-global model or agent for every session, so they are applied to
    this session's route instead. Anything else -- including the bare forms
    -- returns ``None`` and is handled as before.
    """
    parsed = content.parse_prompt(prompt)
    words = parsed.text.strip().split()
    if parsed.attachments or parsed.link_attachments or not words:
        return None
    config_id = {
        "/agent": session_config.AGENT_OPTION_ID,
        "/a": session_config.AGENT_OPTION_ID,
        "/model": session_config.MODEL_OPTION_ID,
        "/m": session_config.MODEL_OPTION_ID,
    }.get(words[0])
    if config_id is None or len(words) != 2:
        return None
    return config_id, words[1]


async def change_route(
    session: ACPSession, config_id: str, value: str, kwargs: Dict[str, Any]
) -> None:
    """Rebuild ``session`` on a changed agent or model, or leave it as is.

    Serialized with the session's turns. Re-selecting the current value is
    a no-op (same epoch). A stale ``expectedRouteEpoch``, a running turn,
    or a value that cannot be built raises, and the session keeps its
    current route. The new route is saved before it is reported.
    """
    async with session.route_lock:
        if session.closing:
            raise ValueError(f"session is closing: {session.session_id}")
        requested_epoch = expected_epoch(kwargs)
        if requested_epoch is not None and requested_epoch != session.route.epoch:
            raise ValueError(
                f"stale route epoch {requested_epoch}; current is {session.route.epoch}"
            )
        if session.is_busy:
            raise ValueError("cannot change route while a prompt is running")
        if config_id == session_config.AGENT_OPTION_ID:
            proposed = session.route.changed(agent_name=value)
        else:
            proposed = session.route.changed(model_id=value)
        if proposed.same_binding(session.route):
            return
        candidate, effective_route = route_runtime.build_agent(
            proposed.agent_name,
            proposed.model_id,
            epoch=proposed.epoch,
            history=list(session.agent.get_message_history()),
            mcp_specs=session.mcp_specs,
        )
        persisted, was_cancelled = await session.persist_settled(
            candidate, effective_route
        )
        if not persisted:
            if was_cancelled:
                raise asyncio.CancelledError
            raise OSError("Could not save the changed session route")
        session.agent = candidate
        session.route = effective_route
        await emit_route_update(session)
        if was_cancelled:
            raise asyncio.CancelledError


async def emit_route_update(session: ACPSession) -> None:
    """Send ``config_option_update`` carrying the new route."""
    connection = state.get_connection()
    if connection is None:
        return
    update = ConfigOptionUpdate(
        session_update="config_option_update",
        config_options=session_config.config_options(session.route),
        field_meta=session.route.metadata(session.session_id),
    )
    try:
        await connection.session_update(session.session_id, update)
    except Exception:  # noqa: BLE001
        logger.debug("ACP: config_option_update failed", exc_info=True)


async def emit_switch_confirmation(session: ACPSession, config_id: str) -> None:
    """Answer a ``/model`` or ``/agent`` prompt with a one-line confirmation."""
    connection = state.get_connection()
    if connection is None:
        return
    from acp.helpers import update_agent_message_text

    value = (
        session.route.agent_name
        if config_id == session_config.AGENT_OPTION_ID
        else session.route.model_id
    )
    try:
        await connection.session_update(
            session.session_id,
            update_agent_message_text(
                f"Switched this session's {config_id} to {value}."
            ),
        )
    except Exception:  # noqa: BLE001
        logger.debug("ACP: route confirmation failed", exc_info=True)
