"""Child-process smoke checks against an extracted wheel, never repo plugins."""

import asyncio
import os
import sys
from importlib import resources
from importlib.metadata import entry_points
from pathlib import Path


class Agent:
    def tool(self, function=None, **kwargs):
        return function if function is not None else lambda function: function


def main():
    installed = Path(sys.argv[1]).resolve()
    # Executing this file normally puts tests/ first. Replace that entry before
    # any plugin/core imports so tests or repository source cannot mask the wheel.
    sys.path[0] = str(installed)
    assert not any(key.startswith("HERDR_") for key in os.environ)
    assert (
        resources.files("code_puppy_core_plugins.herdr").joinpath("SKILL.md").is_file()
    )
    # Load only the relevant wheel entry points; no unrelated auth plugins.
    for name in ("agent_skills", "herdr"):
        entry = next(
            ep for ep in entry_points(group="code_puppy.plugins") if ep.name == name
        )
        assert Path(entry.dist.locate_file("")).resolve() == installed
        entry.load()

    from code_puppy.tools.skills_tools import (
        register_activate_skill,
        register_list_or_search_skills,
    )
    from code_puppy_core_plugins.agent_skills import config, discovery
    from code_puppy_core_plugins.agent_skills.provider import AgentSkillsProvider
    from code_puppy_core_plugins.herdr import register_callbacks as hooks

    assert not hooks._reporter.active
    # Verify the actual imported code, not just the presence of a wheel resource.
    for name, module in tuple(sys.modules.items()):
        if name == "code_puppy_core_plugins" or name.startswith(
            "code_puppy_core_plugins."
        ):
            assert Path(module.__file__).resolve().is_relative_to(installed), name

    config.get_skill_directories = lambda: []
    discovery.get_skill_directories = lambda: []
    discovery.get_default_skill_directories = lambda: []
    activate = register_activate_skill(Agent())
    listing = register_list_or_search_skills(Agent())
    provider = AgentSkillsProvider()
    assert provider.find_enabled_skill_path("herdr-code-puppy") is None
    os.environ.update(HERDR_ENV="1", HERDR_PANE_ID="fake:p1")
    path = provider.find_enabled_skill_path("herdr-code-puppy")
    assert path is not None
    signature = discovery._plugin_skills_signature
    result = asyncio.run(activate(None, "herdr-code-puppy"))
    assert result.error is None, result
    assert "# Herdr Code Puppy" in result.content
    assert result.resources == []
    assert discovery._plugin_skills_signature == signature
    assert asyncio.run(listing(None, "herdr-code-puppy")).total_count == 1
    config.get_disabled_skills = lambda: {"herdr-code-puppy"}
    assert asyncio.run(activate(None, "herdr-code-puppy")).content == ""
    assert asyncio.run(listing(None, "herdr-code-puppy")).total_count == 0
    config.get_disabled_skills = set
    assert asyncio.run(activate(None, "herdr-code-puppy")).error is None
    os.environ.pop("HERDR_ENV")
    assert asyncio.run(activate(None, "herdr-code-puppy")).content == ""
    assert asyncio.run(listing(None, "herdr-code-puppy")).total_count == 0
    assert provider.find_enabled_skill_path("herdr-code-puppy") is None
    assert not path.exists()
    discovery.refresh_skill_cache()
    assert provider.find_enabled_skill_path("herdr-code-puppy") is None


if __name__ == "__main__":
    main()
