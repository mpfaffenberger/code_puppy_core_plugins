"""Tests for the mcp_prompts_resources plugin."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from code_puppy_core_plugins.mcp_prompts_resources import register_callbacks as plugin


class FakeAgent:
    """Captures the functions passed to ``@agent.tool``."""

    def __init__(self):
        self.fns = {}

    def tool(self, fn):
        self.fns[fn.__name__] = fn
        return fn


class FakeToolset:
    def __init__(
        self,
        prompts=None,
        resources=None,
        templates=None,
        body="body",
        caps=None,
        fail=None,
    ):
        self._prompts = prompts or []
        self._resources = resources or []
        self._templates = templates or []
        self._body = body
        self._fail = fail or set()
        self.capabilities = (
            caps if caps is not None else SimpleNamespace(prompts=True, resources=True)
        )

    async def list_prompts(self):
        if "list_prompts" in self._fail:
            raise RuntimeError("boom")
        return self._prompts

    async def get_prompt(self, name, args):
        known = {p.name for p in self._prompts}
        if name not in known:
            raise RuntimeError(f"Unknown prompt: {name}")
        return SimpleNamespace(
            messages=[SimpleNamespace(content=SimpleNamespace(text=f"rendered {name}"))]
        )

    async def list_resources(self):
        if "list_resources" in self._fail:
            raise RuntimeError("boom")
        return self._resources

    async def list_resource_templates(self):
        return self._templates

    async def read_resource(self, uri):
        if uri != "docs://ok":
            raise RuntimeError("Unknown resource")
        return self._body


def _prompt(name, desc="", args=()):
    return SimpleNamespace(
        name=name,
        description=desc,
        arguments=[SimpleNamespace(name=a) for a in args],
    )


@pytest.fixture
def one_server(monkeypatch):
    """Install a single running fake server and return it."""

    def _install(toolset, name="platform"):
        monkeypatch.setattr(plugin, "_running_toolsets", lambda: [(name, toolset)])
        return toolset

    return _install


def _fn(register, name):
    agent = FakeAgent()
    register(agent)
    return agent.fns[name]


# ── registration contract ──────────────────────────────────────


def test_register_tools_contract():
    defs = plugin._register_tools()
    assert {d["name"] for d in defs} == {
        "list_mcp_prompts",
        "get_mcp_prompt",
        "list_mcp_resources",
        "read_mcp_resource",
    }
    assert all(callable(d["register_func"]) for d in defs)


def test_tools_advertised_to_agents():
    assert set(plugin._register_agent_tools("code-puppy")) == set(plugin._TOOLS)


# ── capability gate ────────────────────────────────────────────


def test_supports_false_when_not_connected():
    """A stopped toolset raises AttributeError; that must not propagate."""

    class Stopped:
        @property
        def capabilities(self):
            raise AttributeError("only available after initialization")

    assert plugin._supports(Stopped(), "prompts") is False


def test_supports_reads_advertised_flag():
    ts = FakeToolset(caps=SimpleNamespace(prompts=False, resources=True))
    assert plugin._supports(ts, "prompts") is False
    assert plugin._supports(ts, "resources") is True


# ── prompts ────────────────────────────────────────────────────


async def test_list_prompts_formats_signature(one_server):
    one_server(FakeToolset(prompts=[_prompt("playbook", "Runbook.", ["topic"])]))
    out = await _fn(plugin.register_list_mcp_prompts, "list_mcp_prompts")(None)
    assert out == "platform:playbook(topic) - Runbook."


async def test_list_prompts_empty(one_server):
    one_server(FakeToolset())
    out = await _fn(plugin.register_list_mcp_prompts, "list_mcp_prompts")(None)
    assert out == "No MCP prompts available."


async def test_list_prompts_skips_servers_without_capability(one_server):
    one_server(
        FakeToolset(
            prompts=[_prompt("p")], caps=SimpleNamespace(prompts=False, resources=True)
        )
    )
    out = await _fn(plugin.register_list_mcp_prompts, "list_mcp_prompts")(None)
    assert out == "No MCP prompts available."


async def test_list_prompts_reports_error(one_server):
    one_server(FakeToolset(fail={"list_prompts"}))
    out = await _fn(plugin.register_list_mcp_prompts, "list_mcp_prompts")(None)
    assert "error listing prompts" in out


async def test_get_prompt_bare_and_qualified_name(one_server):
    one_server(FakeToolset(prompts=[_prompt("playbook")]))
    fn = _fn(plugin.register_get_mcp_prompt, "get_mcp_prompt")
    assert await fn(None, "playbook", {}) == "rendered playbook"
    assert await fn(None, "platform:playbook", {}) == "rendered playbook"


async def test_get_prompt_wrong_server_filtered_out(one_server):
    one_server(FakeToolset(prompts=[_prompt("playbook")]))
    fn = _fn(plugin.register_get_mcp_prompt, "get_mcp_prompt")
    assert "not found" in await fn(None, "other:playbook", {})


async def test_get_prompt_surfaces_failure_reason(one_server):
    """A bad-arguments error must not masquerade as a plain 'not found'."""
    one_server(FakeToolset(prompts=[_prompt("playbook")]))
    fn = _fn(plugin.register_get_mcp_prompt, "get_mcp_prompt")
    out = await fn(None, "missing", {})
    assert "not found" in out and "Unknown prompt" in out


# ── resources ──────────────────────────────────────────────────


async def test_list_resources_includes_templates(one_server):
    one_server(
        FakeToolset(
            resources=[SimpleNamespace(uri="docs://ok", description="Runbook")],
            templates=[SimpleNamespace(uriTemplate="docs://{id}", description="By id")],
        )
    )
    out = await _fn(plugin.register_list_mcp_resources, "list_mcp_resources")(None)
    assert "platform docs://ok - Runbook" in out
    assert "docs://{id} (template) - By id" in out


async def test_read_resource_returns_body(one_server):
    one_server(FakeToolset(body="internal runbook"))
    out = await _fn(plugin.register_read_mcp_resource, "read_mcp_resource")(
        None, "docs://ok"
    )
    assert out == "internal runbook"


async def test_read_resource_reports_error(one_server):
    one_server(FakeToolset())
    out = await _fn(plugin.register_read_mcp_resource, "read_mcp_resource")(
        None, "docs://nope"
    )
    assert "Could not read resource" in out and "Unknown resource" in out


# ── server discovery ───────────────────────────────────────────


def test_running_toolsets_skips_stopped(monkeypatch):
    managed = SimpleNamespace(get_pydantic_server=lambda: "TS")
    manager = SimpleNamespace(
        list_servers=lambda: [SimpleNamespace(id="1", name="platform")],
        get_server=lambda _id: managed,
    )
    monkeypatch.setattr(plugin, "get_mcp_manager", lambda: manager)
    monkeypatch.setattr(plugin, "unwrap_toolset", lambda t: t)
    monkeypatch.setattr(plugin, "toolset_is_running", lambda t: False)
    assert plugin._running_toolsets() == []


def test_running_toolsets_skips_unavailable_server(monkeypatch):
    """Disabled/quarantined servers raise from get_pydantic_server()."""

    def boom():
        raise RuntimeError("disabled")

    manager = SimpleNamespace(
        list_servers=lambda: [SimpleNamespace(id="1", name="platform")],
        get_server=lambda _id: SimpleNamespace(get_pydantic_server=boom),
    )
    monkeypatch.setattr(plugin, "get_mcp_manager", lambda: manager)
    assert plugin._running_toolsets() == []
