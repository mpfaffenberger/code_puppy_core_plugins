"""Offline wheel resource and actual discovery/activation smoke test."""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


def _isolated_env(tmp_path):
    env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT")
        if key in os.environ
    }
    for name, directory in {
        "HOME": "home",
        "USERPROFILE": "home",
        "APPDATA": "appdata",
        "LOCALAPPDATA": "localappdata",
        "XDG_CACHE_HOME": "cache",
        "XDG_CONFIG_HOME": "config",
        "XDG_DATA_HOME": "data",
        "XDG_STATE_HOME": "state",
    }.items():
        path = tmp_path / directory
        path.mkdir(exist_ok=True)
        env[name] = str(path)
    env.update(
        PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
        CODE_PUPPY_TELEMETRY_ENABLED="false",
        OTEL_SDK_DISABLED="true",
        LOGFIRE_SEND_TO_LOGFIRE="false",
        DO_NOT_TRACK="1",
        CI="1",
    )
    return env


def test_wheel_gated_discovery_and_activation(tmp_path):
    if shutil.which("uv") is None:
        pytest.skip("uv is required for the offline wheel smoke test")
    env = _isolated_env(tmp_path)
    # A clean runner must not depend on a developer's populated build cache.
    env["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    repo = Path(__file__).resolve().parents[1]
    dist = tmp_path / "dist"
    build = subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--offline",
            "--no-build-isolation",
            "--python",
            sys.executable,
            "--out-dir",
            str(dist),
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stderr or build.stdout
    installed = tmp_path / "installed"
    with zipfile.ZipFile(next(dist.glob("*.whl"))) as archive:
        assert "code_puppy_core_plugins/herdr/SKILL.md" in archive.namelist()
        archive.extractall(installed)
    env["PYTHONPATH"] = str(installed)
    script = r"""
import asyncio
import os
from importlib import resources
from importlib.metadata import entry_points

# Import with HERDR scrubbed: never create live import-time pane authority.
assert not any(key.startswith("HERDR_") for key in os.environ)
assert resources.files("code_puppy_core_plugins.herdr").joinpath("SKILL.md").is_file()
# Load only the relevant entry points; no unrelated provider/auth plugins.
for name in ("agent_skills", "herdr"):
    next(ep for ep in entry_points(group="code_puppy.plugins") if ep.name == name).load()
from code_puppy_core_plugins.herdr import register_callbacks as hooks
assert not hooks._reporter.active
from code_puppy.tools.skills_tools import register_activate_skill, register_list_or_search_skills
from code_puppy_core_plugins.agent_skills import config, discovery
from code_puppy_core_plugins.agent_skills.provider import AgentSkillsProvider

config.get_skill_directories = lambda: []
discovery.get_skill_directories = lambda: []
discovery.get_default_skill_directories = lambda: []
class Agent:
    def tool(self, function=None, **kwargs):
        return function if function is not None else lambda function: function
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
"""
    smoke = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert smoke.returncode == 0, smoke.stderr or smoke.stdout
