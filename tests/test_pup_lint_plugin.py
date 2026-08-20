from __future__ import annotations

from unittest.mock import patch

import pytest

from code_puppy_core_plugins.pup_lint import register_callbacks, runner, tools


class FakeAgent:
    def __init__(self):
        self.registered = {}

    def tool(self, function):
        self.registered[function.__name__] = function
        return function


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.killed = False

    async def communicate(self):
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def test_plugin_is_silent_when_pup_lint_is_unavailable():
    with patch.object(register_callbacks, "available", return_value=False):
        assert register_callbacks._register_tools() == []
        assert register_callbacks._register_agent_tools("code-puppy") == []
        assert register_callbacks._load_prompt() is None


def test_plugin_registers_and_guides_agents_when_available():
    with patch.object(register_callbacks, "available", return_value=True):
        definitions = register_callbacks._register_tools()
        assert definitions == [
            {"name": "pup_lint", "register_func": tools.register_pup_lint}
        ]
        assert register_callbacks._register_agent_tools("code-puppy") == ["pup_lint"]
        prompt = register_callbacks._load_prompt()
        assert prompt is not None
        assert "After creating or changing Python files" in prompt
        assert "diagnostic-only" in prompt


@pytest.mark.asyncio
async def test_registered_tool_delegates_without_exposing_fixes(monkeypatch):
    expected = {"success": True, "clean": True, "diagnostics": []}

    async def fake_lint_paths(paths, **kwargs):
        assert paths == ["src", "tests"]
        assert kwargs == {
            "cwd": "/project",
            "line_length": 100,
            "select": ["E", "F"],
            "ignore": ["E501"],
        }
        return expected

    monkeypatch.setattr(tools, "lint_paths", fake_lint_paths)
    agent = FakeAgent()
    tools.register_pup_lint(agent)

    result = await agent.registered["pup_lint"](
        None,
        ["src", "tests"],
        cwd="/project",
        line_length=100,
        select=["E", "F"],
        ignore=["E501"],
    )

    assert result == expected
    assert "fix" not in agent.registered["pup_lint"].__annotations__


def test_command_prefix_prefers_path_executable(monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/bin/pup-lint")

    assert runner.command_prefix() == ["/bin/pup-lint"]


def test_command_prefix_falls_back_to_current_python(monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    monkeypatch.setattr(runner.importlib.util, "find_spec", lambda name: object())

    assert runner.command_prefix() == [runner.sys.executable, "-m", "pup_lint"]


@pytest.mark.asyncio
async def test_runner_returns_structured_diagnostics(tmp_path, monkeypatch):
    process = FakeProcess(
        b'[{"path":"app.py","line":1,"column":1,"code":"F401","message":"unused"}]',
        returncode=1,
    )
    invocation = {}

    async def fake_subprocess(*argv, **kwargs):
        invocation["argv"] = argv
        invocation["kwargs"] = kwargs
        return process

    monkeypatch.setattr(runner, "command_prefix", lambda: ["/bin/pup-lint"])
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_subprocess)

    result = await runner.lint_paths(
        ["app.py"],
        cwd=str(tmp_path),
        line_length=100,
        select=["F"],
        ignore=["F841"],
    )

    assert result["success"] is True
    assert result["clean"] is False
    assert result["count"] == 1
    assert result["diagnostics"][0]["code"] == "F401"
    assert invocation["argv"] == (
        "/bin/pup-lint",
        "--format",
        "json",
        "--line-length",
        "100",
        "--select",
        "F",
        "--ignore",
        "F841",
        "app.py",
    )
    assert invocation["kwargs"]["cwd"] == tmp_path.resolve()


@pytest.mark.asyncio
async def test_runner_reports_cli_and_json_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "command_prefix", lambda: ["pup-lint"])

    async def cli_failure(*args, **kwargs):
        return FakeProcess(b"", b"bad config", returncode=2)

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", cli_failure)
    result = await runner.lint_paths(["."], cwd=str(tmp_path))
    assert result == {"success": False, "exit_code": 2, "error": "bad config"}

    async def invalid_json(*args, **kwargs):
        return FakeProcess(b"not-json")

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", invalid_json)
    result = await runner.lint_paths(["."], cwd=str(tmp_path))
    assert result["success"] is False
    assert "invalid JSON" in result["error"]


@pytest.mark.asyncio
async def test_runner_validates_environment_and_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "command_prefix", lambda: None)
    result = await runner.lint_paths(["."])
    assert result["success"] is False
    assert "not installed" in result["error"]

    monkeypatch.setattr(runner, "command_prefix", lambda: ["pup-lint"])
    result = await runner.lint_paths(["."], line_length=0)
    assert result == {"success": False, "error": "line_length must be positive."}

    result = await runner.lint_paths(["."], cwd=str(tmp_path / "missing"))
    assert result["success"] is False
    assert "Not a directory" in result["error"]
