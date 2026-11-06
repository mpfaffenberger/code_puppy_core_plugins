"""Folder policy wiring in the file permission prompts (sync and async).

The outside path is a tmp_path sibling that is outside the cwd and outside the
patched temp roots, so it never depends on the real HOME or TMPDIR.
Nothing is created or written, so the path need not exist.
"""

import asyncio
from unittest.mock import patch

import pytest

from code_puppy.tools.subagent_context import is_subagent, subagent_context
from code_puppy_core_plugins.file_permission_handler import folder_policy
from code_puppy_core_plugins.file_permission_handler import register_callbacks as rc

MODES = ["sync", "async"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(folder_policy, "_temp_roots", lambda: [str(tmp_path / "temp")])
    return root


@pytest.fixture
def outside(tmp_path, project):
    return str(tmp_path / "outside" / "notes.md")


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

    def test_edit_outside_always_prompts(self, mode, yolo, outside, prompts):
        assert _prompted(mode, prompts, outside, "replace text in", yolo)

    def test_delete_outside_always_prompts(self, mode, yolo, outside, prompts):
        assert _prompted(mode, prompts, outside, "delete", yolo)

    def test_delete_inside_project_never_prompts(self, mode, yolo, project, prompts):
        target = str(project / "old.txt")
        assert not _prompted(mode, prompts, target, "delete", yolo)

    def test_delete_snippet_outside_always_prompts(self, mode, yolo, outside, prompts):
        assert _prompted(mode, prompts, outside, "delete snippet from", yolo)

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
        ("decision", "expected"),
        [(folder_policy.Decision.ALLOW, True), (folder_policy.Decision.ASK, False)],
    )
    def test_policy_gate_maps_decision(self, decision, expected, monkeypatch):
        monkeypatch.setattr(folder_policy, "decide", lambda *args, **kwargs: decision)
        assert rc._policy_gate("x.py") is expected


def _resolve(where, project, outside):
    paths = {
        "project_src": str(project / "src" / "app.py"),
        "protected": str(project / ".env"),
        "outside": outside,
    }
    return paths[where]


def _run_as_subagent(mode, kind, file_path):
    """Run the real edit/delete handler inside a sub-agent context."""
    with subagent_context("sub-agent"):
        assert is_subagent()
        if kind == "delete":
            if mode == "sync":
                return rc.handle_delete_file_permission(None, file_path)
            return asyncio.run(rc.handle_delete_file_permission_async(None, file_path))
        if mode == "sync":
            return rc.handle_edit_file_permission(
                None, file_path, "write", {"content": "x"}
            )
        return asyncio.run(
            rc.handle_edit_file_permission_async(
                None, file_path, "write", {"content": "x"}
            )
        )


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
    @pytest.mark.parametrize("yolo", [True, False])
    @pytest.mark.parametrize(
        ("kind", "where", "prompt_rule"),
        [
            ("edit", "project_src", "never"),
            ("edit", "protected", "always"),
            ("edit", "outside", "always"),
            ("delete", "outside", "always"),
        ],
    )
    def test_subagent_gets_same_folder_policy(
        self, mode, yolo, kind, where, prompt_rule, project, outside, prompts
    ):
        file_path = _resolve(where, project, outside)
        with patch.object(rc, "get_yolo_mode", return_value=yolo):
            result = _run_as_subagent(mode, kind, file_path)
        expected_prompts = {"never": 0, "always": 1}[prompt_rule]
        assert len(prompts) == expected_prompts
        assert result is (expected_prompts == 0)
