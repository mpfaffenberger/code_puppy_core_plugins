"""Folder policy wiring in the file permission prompts (sync and async).

Outside-approved paths live under the real home directory: the cwd and temp
folders are always approved, so the home path is the only non-approved fixture.
Nothing is created or written, so the path need not exist.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from code_puppy_core_plugins.file_permission_handler import folder_policy
from code_puppy_core_plugins.file_permission_handler import register_callbacks as rc

MODES = ["sync", "async"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def outside():
    return str(Path.home() / ".acp-folder-policy-test" / "notes.md")


@pytest.fixture
def prompts(monkeypatch):
    """Record every approval prompt; the stub denies so confirmed == no-prompt."""
    calls = []

    def sync_stub(**kwargs):
        calls.append(kwargs["title"])
        return False, None

    async def async_stub(**kwargs):
        calls.append(kwargs["title"])
        return False, None

    monkeypatch.setattr(rc, "get_user_approval", sync_stub)
    monkeypatch.setattr(rc, "get_user_approval_async", async_stub)
    return calls


def _run_prompt(mode, file_path, operation, yolo):
    with patch.object(rc, "get_yolo_mode", return_value=yolo):
        if mode == "sync":
            return rc.prompt_for_file_permission(file_path, operation)
        return asyncio.run(rc.prompt_for_file_permission_async(file_path, operation))


def _prompted(mode, prompts, file_path, operation, yolo):
    """Return True when a prompt was shown; confirmed must mirror that."""
    before = len(prompts)
    confirmed, _ = _run_prompt(mode, file_path, operation, yolo)
    prompted = len(prompts) > before
    assert confirmed is (not prompted)
    return prompted


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("yolo", [True, False])
class TestPromptGate:
    def test_edit_inside_project_never_prompts(self, mode, yolo, project, prompts):
        target = str(project / "src" / "app.py")
        assert not _prompted(mode, prompts, target, "write to", yolo)

    @pytest.mark.parametrize("name", [".env", ".git/config", "server.pem"])
    def test_protected_path_in_project_always_prompts(
        self, mode, yolo, name, project, prompts
    ):
        target = str(project / name)
        assert _prompted(mode, prompts, target, "write to", yolo)

    def test_edit_outside_prompts_only_when_not_yolo(
        self, mode, yolo, outside, prompts
    ):
        assert _prompted(mode, prompts, outside, "replace text in", yolo) is not yolo

    def test_delete_outside_always_prompts(self, mode, yolo, outside, prompts):
        assert _prompted(mode, prompts, outside, "delete", yolo)

    def test_delete_inside_project_never_prompts(self, mode, yolo, project, prompts):
        target = str(project / "old.txt")
        assert not _prompted(mode, prompts, target, "delete", yolo)

    def test_delete_snippet_outside_is_an_edit(self, mode, yolo, outside, prompts):
        prompted = _prompted(mode, prompts, outside, "delete snippet from", yolo)
        assert prompted is not yolo

    def test_temp_dir_edit_never_prompts(self, mode, yolo, tmp_path, prompts):
        target = str(tmp_path / "scratch.txt")
        assert not _prompted(mode, prompts, target, "write to", yolo)

    @pytest.mark.parametrize("operation", ["write to", "delete"])
    def test_policy_error_keeps_old_behavior_and_never_allows(
        self, mode, yolo, operation, outside, prompts, monkeypatch
    ):
        def boom(*args, **kwargs):
            raise RuntimeError("policy broke")

        monkeypatch.setattr(folder_policy, "decide", boom)
        assert _prompted(mode, prompts, outside, operation, yolo) is (not yolo)


class TestGateMapping:
    @pytest.mark.parametrize(
        ("operation", "expected_is_delete"),
        [
            ("delete", True),
            ("  DELETE ", True),
            ("delete snippet from", False),
            ("write to", False),
        ],
    )
    def test_is_delete_only_for_exact_delete(
        self, operation, expected_is_delete, monkeypatch
    ):
        seen = {}

        def record(file_path, is_delete=False, cwd=None):
            seen["is_delete"] = is_delete
            return folder_policy.Decision.DEFAULT

        monkeypatch.setattr(folder_policy, "decide", record)
        assert rc._policy_gate("x.py", operation) is None
        assert seen["is_delete"] is expected_is_delete


class TestHandlersAndAgents:
    @pytest.mark.parametrize("mode", MODES)
    def test_delete_handler_prompts_even_in_yolo(self, mode, outside, prompts):
        handler = rc.handle_delete_file_permission
        with patch.object(rc, "get_yolo_mode", return_value=True):
            if mode == "sync":
                assert handler(None, outside) is False
            else:
                async_handler = rc.handle_delete_file_permission_async
                assert asyncio.run(async_handler(None, outside)) is False
        assert len(prompts) == 1

    @pytest.mark.parametrize("mode", MODES)
    def test_gate_ignores_agent_identity(self, mode, outside, prompts):
        """Sub-agent context must not change the outcome; the gate is path-only."""
        sub_agent = SimpleNamespace(agent_name="sub-agent")
        results = []
        for context in (None, sub_agent):
            with patch.object(rc, "get_yolo_mode", return_value=True):
                if mode == "sync":
                    result = rc.handle_edit_file_permission(
                        context, outside, "write", {"content": "x"}
                    )
                else:
                    result = asyncio.run(
                        rc.handle_edit_file_permission_async(
                            context, outside, "write", {"content": "x"}
                        )
                    )
            results.append(result)
        assert results == [True, True]
        assert prompts == []
