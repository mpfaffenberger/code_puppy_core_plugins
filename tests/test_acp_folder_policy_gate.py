"""ACP folder policy gate: every forced prompt reaches the client once, nothing else does.

Runs the real file handlers with core's real approval backend installed via
``permissions.install()``. The fake client counts ``request_permission`` calls, so
each count is one dialog shown over ACP. It always answers allow, so every
operation returns True.
"""

import asyncio
from contextlib import nullcontext
from unittest.mock import patch

import pytest
from acp.schema import AllowedOutcome, RequestPermissionResponse

from code_puppy.tools import common
from code_puppy.tools.subagent_context import is_subagent, subagent_context
from code_puppy_core_plugins.acp import permissions, state
from code_puppy_core_plugins.file_permission_handler import folder_policy
from code_puppy_core_plugins.file_permission_handler import register_callbacks as rc

pytestmark = pytest.mark.asyncio


class _AllowingClient:
    """Stands in for the ACP client: counts dialogs and answers allow_once."""

    def __init__(self):
        self.calls = 0

    async def request_permission(self, options, session_id, tool_call):
        self.calls += 1
        return RequestPermissionResponse(
            outcome=AllowedOutcome(option_id="allow_once", outcome="selected")
        )


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(folder_policy, "_temp_roots", lambda: [str(tmp_path / "temp")])
    monkeypatch.setattr(folder_policy, "get_user_folders", lambda: [])
    return root


@pytest.fixture
def paths(tmp_path, project):
    return {
        "inside": str(project / "src" / "app.py"),
        "protected": str(project / ".env"),
        "outside": str(tmp_path / "outside" / "notes.md"),
        "outside_protected": str(tmp_path / "outside" / ".env"),
    }


@pytest.fixture
def connect():
    def _connect() -> _AllowingClient:
        client = _AllowingClient()
        state.set_connection(client, asyncio.get_running_loop())
        state.begin_run("s1")
        permissions.install()
        assert common.get_approval_backend() is permissions._approval_backend
        return client

    yield _connect
    permissions.uninstall()
    state.end_run()
    state.set_connection(None, None)


async def _write(path):
    return await asyncio.to_thread(
        rc.handle_edit_file_permission, None, path, "write", {"content": "x"}
    )


async def _delete(path):
    return await rc.handle_delete_file_permission_async(None, path)


_OPERATIONS = {"write": _write, "delete": _delete}


def _subagent(enabled: bool):
    return subagent_context("sub") if enabled else nullcontext()


_GATE_CASES = [
    # (path key, operation, dialogs when yolo, dialogs when not yolo)
    pytest.param("inside", "write", 0, 0, id="write-inside-project"),
    pytest.param("protected", "write", 1, 1, id="write-protected-in-project"),
    pytest.param("outside", "delete", 1, 1, id="delete-outside"),
    pytest.param("outside", "write", 1, 1, id="write-outside"),
]


@pytest.mark.parametrize("in_subagent", [False, True], ids=["main", "subagent"])
@pytest.mark.parametrize("yolo", [True, False], ids=["yolo", "no-yolo"])
@pytest.mark.parametrize(
    ("path_key", "operation", "yolo_dialogs", "normal_dialogs"), _GATE_CASES
)
async def test_policy_routes_forced_prompts_through_acp_client(
    path_key,
    operation,
    yolo_dialogs,
    normal_dialogs,
    yolo,
    in_subagent,
    connect,
    paths,
):
    expected = yolo_dialogs if yolo else normal_dialogs
    client = connect()
    with patch.object(rc, "get_yolo_mode", return_value=yolo), _subagent(in_subagent):
        assert is_subagent() is in_subagent
        allowed = await _OPERATIONS[operation](paths[path_key])
    assert allowed is True
    assert client.calls == expected


@pytest.mark.parametrize("yolo", [True, False], ids=["yolo", "no-yolo"])
@pytest.mark.parametrize(
    ("path_key", "expected"),
    [("outside", 0), ("outside_protected", 1)],
    ids=["plain-file", "protected-file"],
)
async def test_user_approved_folder_skips_prompt_but_protected_still_asks(
    path_key, expected, yolo, connect, paths, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        folder_policy, "get_user_folders", lambda: [str(tmp_path / "outside")]
    )
    client = connect()
    with patch.object(rc, "get_yolo_mode", return_value=yolo):
        allowed = await _write(paths[path_key])
    assert allowed is True
    assert client.calls == expected
