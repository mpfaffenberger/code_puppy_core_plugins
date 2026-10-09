"""Compatibility tests for ACP's pydantic-ai 2.31.0 seams."""

from __future__ import annotations

import json
from types import SimpleNamespace

from code_puppy_core_plugins.acp import mcp_config, persistence, replay


def _write_acp_meta(base, session_id: str) -> None:
    (base / f"{session_id}_acp.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "cwd": "/work/project",
                "additional_directories": ["/work/shared"],
                "updated_at": "2026-08-15T12:00:00",
            }
        ),
        encoding="utf-8",
    )


def test_list_persisted_accepts_json_without_pickle(tmp_path):
    base = tmp_path / "acp"
    base.mkdir()
    (base / "json-only.json").write_text(
        json.dumps({"format": 2, "messages": []}), encoding="utf-8"
    )
    _write_acp_meta(base, "json-only")

    records = persistence.list_persisted(base)

    assert [record.session_id for record in records] == ["json-only"]
    assert records[0].cwd == "/work/project"


def test_list_persisted_still_accepts_legacy_pickle(tmp_path):
    base = tmp_path / "acp"
    base.mkdir()
    (base / "legacy.pkl").write_bytes(b"legacy")
    _write_acp_meta(base, "legacy")

    assert [record.session_id for record in persistence.list_persisted(base)] == [
        "legacy"
    ]


def test_acp_mcp_translation_uses_public_toolset_api():
    spec = SimpleNamespace(
        name="local-tools",
        command="python",
        args=["server.py"],
        env=[],
        type=None,
        url=None,
        headers=[],
    )

    translated = mcp_config._translate(spec)

    assert type(translated).__name__ == "PrefixedToolset"
    assert translated.prefix == "local-tools"
    assert type(translated.wrapped).__name__ == "MCPToolset"
    # Core pins this v2 default off to preserve direct-call semantics.
    assert translated.wrapped.prefer_tasks is False


def test_acp_replay_preserves_text_from_unknown_v2_part_kind():
    part = SimpleNamespace(part_kind="speech", transcript="spoken text")

    updates = replay._updates_for_message(SimpleNamespace(parts=[part]))

    assert len(updates) == 1
    assert updates[0].content.text == "spoken text"


def test_acp_replay_skips_unknown_non_text_part_kind():
    part = SimpleNamespace(part_kind="tool-availability-delta", tools_added=["x"])

    assert replay._updates_for_message(SimpleNamespace(parts=[part])) == []


def _stdio_spec(name: str = "local-tools") -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        command="python",
        args=["server.py"],
        env=[],
        type=None,
        url=None,
        headers=[],
    )


def test_acp_mcp_attach_uses_the_agent_runtime_toolsets():
    """Client servers go where the builder merges them on every build."""

    class Agent:
        def __init__(self) -> None:
            self.runtime: list = []
            self._mcp_servers: list = []

        def set_runtime_mcp_toolsets(self, toolsets: list) -> None:
            self.runtime = list(toolsets)

    agent = Agent()

    mcp_config.attach(agent, [_stdio_spec()])

    assert [toolset.prefix for toolset in agent.runtime] == ["local-tools"]
    assert agent._mcp_servers == []


def test_acp_mcp_attach_falls_back_to_mcp_servers_on_older_cores():
    agent = SimpleNamespace(_mcp_servers=["configured"])

    mcp_config.attach(agent, [_stdio_spec()])

    assert agent._mcp_servers[0] == "configured"
    assert len(agent._mcp_servers) == 2
