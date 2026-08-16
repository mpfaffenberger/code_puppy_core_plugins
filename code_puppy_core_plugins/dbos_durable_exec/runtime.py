"""Async context manager wrapping pydantic_agent.run() with a DBOS workflow ID.

pydantic-ai 2.31.0's DBOSAgent converts constructor-level MCP toolsets to
DBOS-safe variants. Runtime private-toolset swaps are intentionally gone;
``mcp_servers`` remains in the callback signature for host compatibility.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from .workflow_ids import generate_dbos_workflow_id


def skip_fallback_render(_agent) -> bool:
    """DBOS renders its own output; tell core to skip the non-streaming fallback.

    Only valid when DBOS is actually launched — otherwise we'd skip the
    fallback render for plain pydantic agents, leaving users with no output.
    """
    from .lifecycle import is_launched

    return is_launched()


@asynccontextmanager
async def dbos_run_context(agent, pydantic_agent, group_id, mcp_servers):
    """Wrap a run() call with SetWorkflowID.

    For sub-agent invocations (group_id starting with 'invoke_agent'), append
    an atomic counter to ensure DBOS workflow ID uniqueness across rapid
    back-to-back calls. For the main agent, use group_id as-is. MCP toolsets
    stay constructor-owned by pydantic-ai's DBOSAgent integration.
    """
    from .lifecycle import is_launched

    if not is_launched():
        # DBOS not launched (e.g. running inside pytest) — be a no-op so the
        # plain pydantic agent run proceeds normally.
        yield
        return

    try:
        from dbos import SetWorkflowID
    except ImportError:
        yield
        return

    workflow_id = (
        generate_dbos_workflow_id(group_id)
        if group_id and group_id.startswith("invoke_agent")
        else group_id
    )

    # ``mcp_servers`` is intentionally unused: DBOSAgent received the
    # constructor-level toolsets and converted them before this context runs.
    # Retain the argument because the core callback ABI supplies it.
    del agent, pydantic_agent, mcp_servers
    with SetWorkflowID(workflow_id):
        yield workflow_id
