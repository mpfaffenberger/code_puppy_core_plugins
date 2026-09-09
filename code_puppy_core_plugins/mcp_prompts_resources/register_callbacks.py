"""Expose MCP prompts and resources to the agent as tools.

Code Puppy consumes MCP tools only. ``MCPToolset`` already implements
``list_prompts``/``get_prompt``/``list_resources``/``read_resource``; this
just surfaces them.

Registered as agent tools rather than slash commands: tool calls run inside
the agent's async context, so the async MCP calls need no sync/async bridge.
"""

from typing import Any, Dict, List, Optional

from code_puppy.callbacks import register_callback
from code_puppy.mcp_.manager import get_mcp_manager
from code_puppy.mcp_.toolset_utils import toolset_is_running, unwrap_toolset


def _running_toolsets() -> List[tuple]:
    """Yield ``(server_name, leaf_toolset)`` for every running MCP server."""
    out = []
    manager = get_mcp_manager()
    for info in manager.list_servers():
        managed = manager.get_server(info.id)
        if managed is None:
            continue
        try:
            toolset = unwrap_toolset(managed.get_pydantic_server())
        except Exception:
            # Disabled or quarantined servers raise; skip them.
            continue
        if toolset_is_running(toolset):
            out.append((info.name, toolset))
    return out


def _supports(toolset: Any, field: str) -> bool:
    """Whether the server advertised a capability. Unknown -> assume yes."""
    try:
        caps = toolset.capabilities
    except AttributeError:
        return False
    return bool(getattr(caps, field, True))


def _text_of(content: Any) -> str:
    """Best-effort text for a prompt message or resource payload."""
    for attr in ("text", "content", "data"):
        value = getattr(content, attr, None)
        if isinstance(value, str):
            return value
        if value is not None and not isinstance(value, (bytes, bytearray)):
            inner = getattr(value, "text", None)
            if isinstance(inner, str):
                return inner
    if isinstance(content, str):
        return content
    return repr(content)


def register_list_mcp_prompts(agent):
    @agent.tool
    async def list_mcp_prompts(context) -> str:
        """List prompts offered by running MCP servers."""
        lines: List[str] = []
        for name, toolset in _running_toolsets():
            if not _supports(toolset, "prompts"):
                continue
            try:
                prompts = await toolset.list_prompts()
            except Exception as e:
                lines.append(f"{name}: error listing prompts: {e}")
                continue
            for p in prompts:
                args = ", ".join(
                    getattr(a, "name", "")
                    for a in (getattr(p, "arguments", None) or [])
                )
                desc = getattr(p, "description", "") or ""
                sig = f"({args})" if args else "()"
                lines.append(f"{name}:{p.name}{sig} - {desc}".rstrip(" -"))
        return "\n".join(lines) if lines else "No MCP prompts available."

    return list_mcp_prompts


def register_get_mcp_prompt(agent):
    @agent.tool
    async def get_mcp_prompt(
        context, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> str:
        """Render an MCP prompt. ``name`` is ``server:prompt`` or just ``prompt``."""
        server_filter, _, prompt_name = name.rpartition(":")
        errors: List[str] = []
        for server_name, toolset in _running_toolsets():
            if server_filter and server_name != server_filter:
                continue
            if not _supports(toolset, "prompts"):
                continue
            try:
                result = await toolset.get_prompt(prompt_name, arguments or {})
            except Exception as e:
                # Could be "wrong server" or genuinely bad arguments; keep the
                # message so the latter doesn't masquerade as "not found".
                errors.append(f"{server_name}: {e}")
                continue
            parts = [
                _text_of(getattr(m, "content", m))
                for m in (getattr(result, "messages", None) or [])
            ]
            return "\n\n".join(p for p in parts if p) or "(prompt returned no content)"
        detail = f" ({'; '.join(errors)})" if errors else ""
        return f"Prompt not found on any running MCP server: {name}{detail}"

    return get_mcp_prompt


def register_list_mcp_resources(agent):
    @agent.tool
    async def list_mcp_resources(context) -> str:
        """List resources and resource templates on running MCP servers."""
        lines: List[str] = []
        for name, toolset in _running_toolsets():
            if not _supports(toolset, "resources"):
                continue
            try:
                resources = await toolset.list_resources()
            except Exception as e:
                lines.append(f"{name}: error listing resources: {e}")
                resources = []
            for r in resources:
                desc = getattr(r, "description", "") or getattr(r, "name", "") or ""
                lines.append(f"{name} {r.uri} - {desc}".rstrip(" -"))
            try:
                templates = await toolset.list_resource_templates()
            except Exception:
                templates = []
            for t in templates:
                uri = getattr(t, "uriTemplate", None) or getattr(t, "uri_template", "")
                desc = getattr(t, "description", "") or ""
                lines.append(f"{name} {uri} (template) - {desc}".rstrip(" -"))
        return "\n".join(lines) if lines else "No MCP resources available."

    return list_mcp_resources


def register_read_mcp_resource(agent):
    @agent.tool
    async def read_mcp_resource(context, uri: str) -> str:
        """Read an MCP resource by URI."""
        errors: List[str] = []
        for name, toolset in _running_toolsets():
            if not _supports(toolset, "resources"):
                continue
            try:
                contents = await toolset.read_resource(uri)
            except Exception as e:
                errors.append(f"{name}: {e}")
                continue
            if isinstance(contents, (list, tuple)):
                body = "\n".join(_text_of(c) for c in contents)
            else:
                body = _text_of(contents)
            return body or "(resource is empty)"
        detail = f" ({'; '.join(errors)})" if errors else ""
        return f"Could not read resource: {uri}{detail}"

    return read_mcp_resource


_TOOLS = {
    "list_mcp_prompts": register_list_mcp_prompts,
    "get_mcp_prompt": register_get_mcp_prompt,
    "list_mcp_resources": register_list_mcp_resources,
    "read_mcp_resource": register_read_mcp_resource,
}


def _register_tools() -> List[Dict[str, Any]]:
    return [{"name": n, "register_func": f} for n, f in _TOOLS.items()]


def _register_agent_tools(agent_name=None) -> List[str]:
    return list(_TOOLS)


register_callback("register_tools", _register_tools)
register_callback("register_agent_tools", _register_agent_tools)
