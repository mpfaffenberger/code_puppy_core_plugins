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
    script = Path(__file__).with_name("herdr_skill_wheel_support.py")
    smoke = subprocess.run(
        [sys.executable, str(script), str(installed)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert smoke.returncode == 0, smoke.stderr or smoke.stdout
