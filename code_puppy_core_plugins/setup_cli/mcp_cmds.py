"""``setup mcp`` — MCP server registry + agent bindings, scriptably."""

from __future__ import annotations

from typing import Any, Optional

from .output import Out, SetupError


def _summarise(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    if cfg.get("url"):
        transport, target = "http", str(cfg["url"])
    else:
        transport = "stdio"
        target = " ".join(
            [str(cfg.get("command", "")), *[str(a) for a in cfg.get("args", [])]]
        ).strip()
    return {
        "name": name,
        "transport": transport,
        "target": target,
        "enabled": cfg.get("enabled", True),
    }


def cmd_mcp_list(out: Out) -> int:
    from code_puppy.config import load_mcp_server_configs
    from code_puppy.mcp_.agent_bindings import load_bindings

    servers = load_mcp_server_configs() or {}
    bindings = load_bindings()
    bound_to: dict[str, list[str]] = {}
    for agent, server_map in bindings.items():
        for server in server_map:
            bound_to.setdefault(server, []).append(agent)
    rows = [
        {**_summarise(name, cfg), "bound_to": bound_to.get(name, [])}
        for name, cfg in sorted(servers.items())
        if isinstance(cfg, dict)
    ]
    human = [
        f"{row['name']}  [{row['transport']}] {row['target']}"
        + (f"  -> {', '.join(row['bound_to'])}" if row["bound_to"] else "  (unbound)")
        for row in rows
    ] or ["(no MCP servers configured)"]
    out.result({"servers": rows}, human=human)
    return 0


def cmd_mcp_add(
    out: Out,
    name: str,
    command: Optional[str],
    args: list[str],
    url: Optional[str],
    env_pairs: list[str],
    bind_agent: Optional[str],
) -> int:
    if bool(command) == bool(url):
        raise SetupError("pass exactly one of --command or --url")
    server_config: dict[str, Any] = {}
    if url:
        server_config["url"] = url
    else:
        server_config["command"] = command
        if args:
            server_config["args"] = args
    env: dict[str, str] = {}
    for pair in env_pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SetupError(f"invalid --env '{pair}', expected KEY=VALUE")
        env[key] = value
    if env:
        server_config["env"] = env

    from code_puppy.command_line.mcp.mcp_servers_store import upsert_mcp_server

    upsert_mcp_server(name, server_config)
    if bind_agent:
        from code_puppy.mcp_.agent_bindings import set_binding

        set_binding(bind_agent, name, auto_start=True)
    out.result(
        {"name": name, "config": server_config, "bound_to": bind_agent},
        human=f"saved MCP server '{name}'"
        + (f", bound to agent '{bind_agent}'" if bind_agent else ""),
    )
    return 0


def cmd_mcp_remove(out: Out, name: str) -> int:
    from code_puppy.command_line.mcp.mcp_servers_store import remove_mcp_server
    from code_puppy.mcp_.agent_bindings import load_bindings, remove_binding

    if not remove_mcp_server(name):
        raise SetupError(f"MCP server '{name}' not found")
    for agent, server_map in load_bindings().items():
        if name in server_map:
            remove_binding(agent, name)
    out.result({"name": name, "removed": True}, human=f"removed MCP server '{name}'")
    return 0


def cmd_mcp_unbind(out: Out, agent: str, server: str) -> int:
    from code_puppy.mcp_.agent_bindings import remove_binding

    if not remove_binding(agent, server):
        raise SetupError(f"'{server}' is not bound to agent '{agent}'")
    out.result(
        {"agent": agent, "server": server, "unbound": True},
        human=f"unbound '{server}' from agent '{agent}'",
    )
    return 0


def cmd_mcp_bind(out: Out, agent: str, server: str, no_auto_start: bool) -> int:
    from code_puppy.mcp_.agent_bindings import set_binding

    set_binding(agent, server, auto_start=not no_auto_start)
    out.result(
        {"agent": agent, "server": server, "auto_start": not no_auto_start},
        human=f"bound '{server}' to agent '{agent}'",
    )
    return 0
