"""Tests for the bundled Agent Creator delegation skill."""

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from code_puppy.callbacks import get_callbacks, register_callback
from code_puppy_core_plugins.agent_creator_skill.register_callbacks import (
    _register_agent_creator_skill,
)
from code_puppy_core_plugins.agent_skills import parse_yaml_frontmatter
from code_puppy_core_plugins.agent_skills.metadata import parse_skill_metadata

pytestmark = pytest.mark.plugin_skills


def _skill_content() -> str:
    path = Path(_register_agent_creator_skill()[0]["skill_md_path"])
    return path.read_text(encoding="utf-8")


def test_registration_matches_frontmatter() -> None:
    entry = _register_agent_creator_skill()[0]
    metadata = parse_yaml_frontmatter(_skill_content())

    assert entry["name"] == metadata["name"] == "agent-creator"
    assert "Spill configuration" in metadata["description"]
    parsed = parse_skill_metadata(Path(entry["skill_md_path"]).parent)
    assert parsed is not None
    assert parsed.tags == ["agents", "json", "tools", "spill"]
    assert 'invoke_agent(agent_name="agent-creator"' in _skill_content()


def test_schema_security_and_spill_contracts_are_documented() -> None:
    body = " ".join(_skill_content().split())

    assert "`name`, `description`, `system_prompt`, and `tools`" in body
    assert "Omit `tools_config.spill` to inherit global Spill behavior" in body
    assert "Only the literal JSON boolean `false` opts that agent out" in body
    assert "`skip_tools` must be a list of non-empty tool-name strings" in body
    assert "Per-agent `skip_tools` is **additive**" in body
    assert "Globally configuring `spill_skip_tools` replaces the default set" in body
    assert "`true` cannot force Spill on" in body
    assert "concurrent agents may use different Spill policies" in body
    assert "A Spill exemption changes result handling, not tool authorization" in body
    assert "Never place passwords, tokens, cookies, private keys" in body
    assert "Optional `model` omitted unless explicitly selected" in body
    assert "project scope" in body
    assert "machine bindings override them" in body
    assert "does **not** fully validate" in body
    assert "real runtime smoke test" in body


def test_built_wheel_loads_and_activates_skill(tmp_path) -> None:
    repo_root = Path(__file__).parents[1]
    dist_dir = tmp_path / "dist"
    build = subprocess.run(
        ["uv", "build", "--wheel", "--offline", "--out-dir", str(dist_dir)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert build.returncode == 0, build.stderr or build.stdout

    wheel = next(dist_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "code_puppy_core_plugins/agent_creator_skill/SKILL.md" in names
    assert any(name.endswith(".dist-info/entry_points.txt") for name in names)

    install_dir = tmp_path / "installed"
    install = subprocess.run(
        ["uv", "pip", "install", "--target", str(install_dir), "--no-deps", str(wheel)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert install.returncode == 0, install.stderr or install.stdout

    script = """
import os
from importlib import resources
from importlib.metadata import entry_points

entry_point = next(
    ep for ep in entry_points(group="code_puppy.plugins")
    if ep.name == "agent_creator_skill"
    and str(ep.dist.locate_file("")).startswith(os.environ["WHEEL_INSTALL_DIR"])
)
from code_puppy.plugins import load_plugin_callbacks

loaded = load_plugin_callbacks()
assert "agent_creator_skill" in loaded["builtin"], loaded
assert resources.files("code_puppy_core_plugins.agent_creator_skill").joinpath(
    "SKILL.md"
).is_file()

from code_puppy_core_plugins.agent_skills import discovery
from code_puppy_core_plugins.agent_skills.provider import AgentSkillsProvider

matches = [s for s in discovery._collect_plugin_skills() if s.name == "agent-creator"]
assert len(matches) == 1, matches
content = AgentSkillsProvider().load_skill_content(matches[0].path)
assert content is not None
assert 'invoke_agent(agent_name="agent-creator"' in content
"""
    inherited = (
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    )
    env = {key: os.environ[key] for key in inherited if key in os.environ}
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PYTHONPATH": str(install_dir),
            "WHEEL_INSTALL_DIR": str(install_dir),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_skill_is_discoverable_and_activatable(tmp_path, monkeypatch) -> None:
    from code_puppy_core_plugins.agent_skills import discovery
    from code_puppy_core_plugins.agent_skills.provider import AgentSkillsProvider

    register_callback("register_skills", _register_agent_creator_skill)
    assert _register_agent_creator_skill in get_callbacks("register_skills")
    monkeypatch.setattr(discovery, "_PLUGIN_SKILLS_CACHE_DIR", tmp_path / "skills")
    discovery._plugin_skills_cache = None
    discovery._plugin_skills_signature = None

    try:
        matches = [
            skill
            for skill in discovery._collect_plugin_skills()
            if skill.name == "agent-creator"
        ]
        assert len(matches) == 1
        content = AgentSkillsProvider().load_skill_content(matches[0].path)
        assert content is not None
        assert parse_yaml_frontmatter(content)["name"] == "agent-creator"
    finally:
        discovery._plugin_skills_cache = None
        discovery._plugin_skills_signature = None
