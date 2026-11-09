"""Cross-platform facts, entry-point discovery, and real core prompt wiring."""

import importlib
from importlib.metadata import entry_points
from unittest.mock import Mock, patch

import pytest

from code_puppy import callbacks
from code_puppy_core_plugins.execution_environment import register_callbacks as plugin


@pytest.fixture
def posix_host(monkeypatch):
    monkeypatch.setattr(plugin.sys, "platform", "linux")
    monkeypatch.delattr(plugin.sys, "getandroidapilevel", raising=False)


@pytest.mark.parametrize("system", ["Linux", "Darwin", "FreeBSD", "CYGWIN_NT-10.0"])
def test_posix_uses_sh_not_terminal(monkeypatch, posix_host, system):
    monkeypatch.setattr(plugin.platform, "system", lambda: system)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    text = plugin._on_load_prompt()
    assert f"Operating system: {system}" in text
    assert "Command shell: /bin/sh" in text
    assert "/bin/zsh" not in text
    assert "GNU-only" in text


def test_windows_ignores_powershell_or_git_bash_terminal(monkeypatch):
    monkeypatch.setattr(plugin.platform, "system", lambda: "Windows")
    monkeypatch.setattr(plugin.sys, "platform", "win32")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setenv("SHELL", "/usr/bin/bash")
    text = plugin._on_load_prompt()
    assert "Operating system: Windows" in text
    assert r"Command shell: C:\Windows\System32\cmd.exe" in text
    assert "%NAME%" in text
    assert "do not use PowerShell cmdlets" in text
    assert "remote hosts" in text


@pytest.mark.parametrize("comspec", [None, ""])
def test_windows_fallback(monkeypatch, comspec):
    monkeypatch.setattr(plugin.sys, "platform", "win32")
    monkeypatch.delenv("COMSPEC", raising=False)
    if comspec is not None:
        monkeypatch.setenv("COMSPEC", comspec)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    assert "cmd.exe" in plugin._on_load_prompt()


def test_android_shell(monkeypatch, posix_host):
    monkeypatch.setattr(plugin.sys, "getandroidapilevel", lambda: 35, raising=False)
    assert "Command shell: /system/bin/sh" in plugin._on_load_prompt()


def test_unknown_os_fallback(monkeypatch, posix_host):
    monkeypatch.setattr(plugin.platform, "system", lambda: "")
    assert "Operating system: linux" in plugin._on_load_prompt()


def test_provider_recomputes_instead_of_freezing_at_import(monkeypatch, posix_host):
    system = Mock(side_effect=["Linux", "Darwin"])
    monkeypatch.setattr(plugin.platform, "system", system)
    assert "Operating system: Linux" in plugin._on_load_prompt()
    assert "Operating system: Darwin" in plugin._on_load_prompt()


def test_detection_failure_skips_fragment(monkeypatch):
    monkeypatch.setattr(
        plugin.platform, "system", Mock(side_effect=OSError("unavailable"))
    )
    assert plugin._on_load_prompt() is None


def test_entry_point_registers_additive_load_prompt(monkeypatch):
    registered = Mock()
    monkeypatch.setattr(callbacks, "register_callback", registered)
    entry = next(
        ep
        for ep in entry_points(group="code_puppy.plugins")
        if ep.name == "execution_environment"
    )
    module = entry.load()
    importlib.reload(module)
    registered.assert_called_with("load_prompt", module._on_load_prompt)


def test_unmodified_core_receives_environment_as_dynamic_instructions(monkeypatch):
    from code_puppy.agents.agent_code_puppy import CodePuppyAgent
    from code_puppy.model_utils import static_then_runtime

    monkeypatch.setitem(callbacks._callbacks, "load_prompt", [plugin._on_load_prompt])
    agent = CodePuppyAgent()
    runtime = agent.get_runtime_system_prompt()
    assert "Operating system:" in runtime
    assert "Command shell:" in runtime
    assert agent.get_identity() in runtime
    authored = agent.get_system_prompt()
    assert "Execution environment (local shell tool):" not in authored
    static, dynamic = static_then_runtime(authored, runtime)
    assert static == authored
    assert dynamic() == runtime
    assert agent.get_full_system_prompt() == authored + runtime


@pytest.mark.asyncio
async def test_real_builder_sends_plugin_fragment_to_model(monkeypatch):
    from pydantic_ai.models.function import FunctionModel

    from code_puppy.agents import _builder
    from code_puppy.agents.agent_code_puppy import CodePuppyAgent

    monkeypatch.setitem(callbacks._callbacks, "load_prompt", [plugin._on_load_prompt])
    seen = []

    async def respond(messages, info):
        seen.extend(info.model_request_parameters.instruction_parts or [])
        yield "done"

    model = FunctionModel(stream_function=respond)
    with (
        patch.object(
            _builder, "load_model_with_fallback", lambda *a, **k: (model, "test")
        ),
        patch.object(_builder.ModelFactory, "load_config", staticmethod(dict)),
        patch.object(_builder, "load_mcp_servers", lambda **k: []),
        patch.object(_builder, "make_model_settings", lambda *a, **k: None),
    ):
        built = _builder.build_pydantic_agent(CodePuppyAgent())
    await built.run("Describe your execution environment")
    dynamic = "".join(part.content for part in seen if part.dynamic)
    assert "Operating system:" in dynamic
    assert "Command shell:" in dynamic
    assert not any(
        "Execution environment (local shell tool):" in part.content
        for part in seen
        if not part.dynamic
    )
