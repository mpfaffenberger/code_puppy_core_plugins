"""Gated bundled guidance: fake environment, no live pane transport."""

from pathlib import Path

import pytest

from code_puppy.callbacks import get_callbacks
from code_puppy_core_plugins.agent_skills import discovery, parse_yaml_frontmatter
from code_puppy_core_plugins.agent_skills.provider import AgentSkillsProvider
from code_puppy_core_plugins.herdr import register_callbacks as hooks

pytestmark = pytest.mark.plugin_skills


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "_PLUGIN_SKILLS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(discovery, "_plugin_skills_cache", None)
    monkeypatch.setattr(discovery, "_plugin_skills_signature", None)
    monkeypatch.setattr(discovery, "get_skill_directories", lambda: [])
    monkeypatch.setattr(discovery, "get_default_skill_directories", lambda: [])
    from code_puppy import callbacks

    # Keep real callback dispatch and signature/materialization semantics.
    monkeypatch.setitem(
        callbacks._callbacks, "register_skills", [hooks._register_herdr_skill]
    )
    return AgentSkillsProvider()


@pytest.mark.parametrize(
    "env,pane",
    [(None, None), ("1", None), ("0", "fake:p1"), ("true", "fake:p1"), ("1", "")],
)
def test_registration_absent_outside_context(monkeypatch, env, pane):
    for key, value in (("HERDR_ENV", env), ("HERDR_PANE_ID", pane)):
        monkeypatch.delenv(key, raising=False)
        if value is not None:
            monkeypatch.setenv(key, value)
    assert hooks._register_herdr_skill() == []
    assert hooks._launcher_prompt() is None


def test_runtime_gate_name_and_file(monkeypatch):
    assert hooks._register_herdr_skill in get_callbacks("register_skills")
    assert hooks._register_herdr_skill() == []
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "fake:p1")
    (entry,) = hooks._register_herdr_skill()
    content = Path(entry["skill_md_path"]).read_text(encoding="utf-8")
    assert (
        entry["name"] == parse_yaml_frontmatter(content)["name"] == "herdr-code-puppy"
    )
    assert "activate_skill" in hooks._launcher_prompt()
    assert "herdr-code-puppy" in hooks._launcher_prompt()


@pytest.mark.parametrize("refresh", [False, True])
def test_warm_catalog_inside_to_outside(catalog, monkeypatch, refresh):
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "fake:p1")
    path = catalog.find_enabled_skill_path("herdr-code-puppy")
    assert path is not None
    signature = discovery._plugin_skills_signature
    assert catalog.find_enabled_skill_path("herdr-code-puppy") == path
    assert discovery._plugin_skills_signature == signature
    assert catalog.get_skill_resources(path) == []
    assert "# Herdr Code Puppy" in catalog.load_skill_content(path)
    monkeypatch.delenv("HERDR_PANE_ID")
    if refresh:
        discovery.refresh_skill_cache()
    assert catalog.find_enabled_skill_path("herdr-code-puppy") is None
    assert catalog.list_enabled_skills() == []
    assert discovery.discover_skills([]) == []
    assert discovery._plugin_skills_signature != signature
    assert not path.exists()


def test_disabled_skill_is_not_activatable(catalog, monkeypatch):
    from code_puppy_core_plugins.agent_skills import config

    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "fake:p1")
    assert catalog.find_enabled_skill_path("herdr-code-puppy") is not None
    monkeypatch.setattr(config, "get_disabled_skills", lambda: {"herdr-code-puppy"})
    assert catalog.find_enabled_skill_path("herdr-code-puppy") is None
    assert catalog.list_enabled_skills() == []
    assert [s.name for s in discovery.discover_skills([])] == ["herdr-code-puppy"]
    monkeypatch.delenv("HERDR_ENV")
    assert discovery.discover_skills([]) == []


@pytest.mark.asyncio
async def test_real_activation_tool(catalog, monkeypatch):
    from code_puppy.tools import skills_tools

    class Agent:
        def tool(self, function=None, **kwargs):
            return function if function is not None else lambda function: function

    monkeypatch.setattr(skills_tools, "get_skill_provider", lambda: catalog)
    activate = skills_tools.register_activate_skill(Agent())
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "fake:p1")
    result = await activate(None, "herdr-code-puppy")
    assert result.error is None
    assert "# Herdr Code Puppy" in result.content
    assert result.resources == []
    monkeypatch.delenv("HERDR_ENV")
    result = await activate(None, "herdr-code-puppy")
    assert result.content == ""
    assert "not found or disabled" in result.error
