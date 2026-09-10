from __future__ import annotations

import asyncio

import pytest

from code_puppy_core_plugins.pup_lint import runner


class HangingProcess:
    def __init__(self):
        self.returncode = None
        self.killed = False

    async def communicate(self):
        await asyncio.Event().wait()

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_timeout_kills_child_process(tmp_path, monkeypatch):
    process = HangingProcess()

    async def fake_subprocess(*args, **kwargs):
        return process

    monkeypatch.setattr(runner, "command_prefix", lambda: ["pup-lint"])
    monkeypatch.setattr(runner, "TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_subprocess)

    result = await runner.lint_paths(["."], cwd=str(tmp_path))

    assert process.killed is True
    assert result["success"] is False
    assert "timed out" in result["error"]


@pytest.mark.asyncio
async def test_cancellation_kills_child_process(tmp_path, monkeypatch):
    process = HangingProcess()

    async def fake_subprocess(*args, **kwargs):
        return process

    monkeypatch.setattr(runner, "command_prefix", lambda: ["pup-lint"])
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_subprocess)
    task = asyncio.create_task(runner.lint_paths(["."], cwd=str(tmp_path)))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed is True
