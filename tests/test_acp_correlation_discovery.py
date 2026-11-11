"""Installed entry-point discovery and actual server construction select this bridge."""

import os
from pathlib import Path
import subprocess
import sys


SCRIPT = r"""
import asyncio
from importlib.metadata import entry_points
from pathlib import Path
from unittest.mock import patch
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from code_puppy import callbacks, plugins, pydantic_patches

assert any(point.name == "acp" for point in entry_points(group=plugins.PLUGIN_ENTRY_POINT_GROUP))
with (
    patch.object(plugins, "USER_PLUGINS_DIR", Path.home() / "plugins"),
    patch.object(plugins, "get_project_plugins_directory", return_value=None),
    patch("code_puppy.model_factory.ModelFactory.load_config", return_value={}),
):
    loaded = plugins.load_plugin_callbacks()
assert loaded["builtin"].count("acp") == 1
from code_puppy_core_plugins.acp import register_callbacks as startup, state
from code_puppy_core_plugins.acp.bridge import EventBridge
handles = [callback for callback in callbacks.get_callbacks("handle_cli_args")
           if callback.__module__.endswith("acp.register_callbacks")]
assert len(handles) == 1 and handles[0].__module__ == startup.__name__
for phase in ("pre_tool_call", "post_tool_call"):
    callbacks.clear_callbacks(phase)
assert pydantic_patches.patch_tool_call_callbacks()

async def main():
    async def serve(agent):
        assert type(agent._bridge) is EventBridge
        updates = []
        class Connection:
            async def session_update(self, session_id, update):
                assert session_id == "installed-session"
                updates.append(update)
        state.begin_run("installed-session")
        agent.on_connect(Connection())
        try:
            # Runtime-ID-capable core: copied arguments, FIFO completion.
            for identifier in ("first", "second"):
                await callbacks.on_pre_tool_call("operation", {"command": identifier}, {"tool_call_id": identifier})
            for identifier in ("first", "second"):
                await callbacks.on_post_tool_call("operation", {"command": identifier}, "output-" + identifier, 1.0, {"tool_call_id": identifier, "outcome": "ok"})
            starts = {item.tool_call_id: item for item in updates if item.status == "in_progress"}
            ends = {item.tool_call_id: item for item in updates if item.status != "in_progress"}
            assert len(starts) == len(ends) == 2
            for identifier, card in starts.items():
                assert card.raw_input == {} and not card.locations
                assert ends[identifier].raw_output == "output-" + ends[identifier].raw_input["command"]
            assert state.current_tool_call() is None
            # Current legacy core: real Agent/ToolManager dispatch without IDs.
            updates.clear()
            async def operation(command: str):
                return "output-" + command
            def model(messages, info):
                if len(messages) == 1:
                    return ModelResponse(parts=[ToolCallPart("operation", {"command": "legacy"}, tool_call_id="legacy-runtime")])
                return ModelResponse(parts=[TextPart("done")])
            await Agent(FunctionModel(model), tools=[operation]).run("run")
            assert len(updates) == 2
            assert updates[0].tool_call_id == updates[1].tool_call_id
            assert updates[1].raw_output == "output-legacy"
            assert state.current_tool_call() is None
        finally:
            state.end_run()
    with (
        patch("acp.run_agent", side_effect=serve),
        patch("code_puppy_core_plugins.acp.permissions.install"),
        patch("code_puppy_core_plugins.acp.permissions.uninstall"),
        patch("code_puppy_core_plugins.acp.io_delegation.uninstall"),
        patch("code_puppy_core_plugins.acp.feedback.schedule_processing"),
        patch("code_puppy_core_plugins.acp.themes.ThemeWatcher.start"),
    ):
        assert await startup._serve() == 0
    assert state.get_connection() is None
    assert not any(getattr(callback, "__self__", None) for callback in callbacks.get_callbacks("post_tool_call"))
asyncio.run(main())
print("INSTALLED_ACP_CORRELATION_OK")
"""


def test_normal_discovery_and_server_constructor(tmp_path):
    environment = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "PYTHONPATH")
        if key in os.environ
    }
    environment.update(
        HOME=str(tmp_path),
        USERPROFILE=str(tmp_path),
        NO_VERSION_UPDATE="1",
        PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
    )
    for category in ("CONFIG", "DATA", "CACHE", "STATE"):
        environment[f"XDG_{category}_HOME"] = str(tmp_path / category.lower())
    guard = "import socket\ndef denied(*a, **k): raise AssertionError('unexpected network')\n"
    guard += "socket.socket.connect = socket.socket.connect_ex = socket.create_connection = socket.getaddrinfo = denied\n"
    result = subprocess.run(
        [sys.executable, "-c", guard + SCRIPT],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "INSTALLED_ACP_CORRELATION_OK" in result.stdout
