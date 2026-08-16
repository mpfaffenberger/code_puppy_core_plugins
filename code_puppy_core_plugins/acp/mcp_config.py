"""Attach client-injected MCP servers (from ``session/new``) to a session agent.

ACP lets the client pass MCP server specs per session. We translate each into
the pydantic-ai v2 ``MCPToolset`` API and append it to the host agent's
MCP configuration; the session agent owns their lifecycle. Best-effort: a
malformed spec is skipped, never fatal.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _kv(items: Any) -> Dict[str, str]:
    """Turn a list of ``{name, value}`` models into a plain dict."""
    out: Dict[str, str] = {}
    for item in items or []:
        name = getattr(item, "name", None)
        if name is not None:
            out[name] = getattr(item, "value", "") or ""
    return out


def _with_prefix(toolset: Any, name: Optional[str]) -> Any:
    """Prefix tool names through the public toolset wrapper API."""
    return toolset.prefixed(name) if name else toolset


def _translate(spec: Any) -> Optional[Any]:
    """Map one ACP MCP server spec to a pydantic-ai MCP toolset."""
    from fastmcp.client.transports import (
        SSETransport,
        StdioTransport,
        StreamableHttpTransport,
    )
    from pydantic_ai.mcp import MCPToolset

    name = getattr(spec, "name", None) or None
    if getattr(spec, "command", None):
        transport = StdioTransport(
            command=spec.command,
            args=list(getattr(spec, "args", None) or []),
            env=_kv(getattr(spec, "env", None)) or None,
        )
        return _with_prefix(MCPToolset(transport, prefer_tasks=False), name)
    url = getattr(spec, "url", None)
    if not url:
        return None
    headers = _kv(getattr(spec, "headers", None)) or None
    if getattr(spec, "type", None) == "sse":
        transport = SSETransport(url=url, headers=headers)
    else:
        transport = StreamableHttpTransport(url=url, headers=headers)
    return _with_prefix(MCPToolset(transport, prefer_tasks=False), name)


def attach(agent: Any, specs: List[Any]) -> None:
    """Append the translated MCP servers from ``specs`` to ``agent``."""
    servers: List[Any] = []
    for spec in specs or []:
        try:
            translated = _translate(spec)
        except Exception:  # noqa: BLE001
            logger.debug("ACP: could not translate MCP server spec", exc_info=True)
            continue
        if translated is not None:
            servers.append(translated)
    if not servers:
        return
    existing = getattr(agent, "_mcp_servers", None) or []
    agent._mcp_servers = list(existing) + servers
