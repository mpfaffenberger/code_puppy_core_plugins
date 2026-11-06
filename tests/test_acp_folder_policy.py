"""ACP approved-folder extension methods, driven through the SDK's wire router.

Config is an in-memory fake: ``get_value`` / ``set_config_value`` never touch the
real ``puppy.cfg``, and ``HOME`` points at a scratch folder so ``Path.home()``
is deterministic.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest
from acp import RequestError
from acp.agent.router import build_agent_router

from code_puppy_core_plugins.acp import io_delegation, permissions
from code_puppy_core_plugins.acp.agent import CodePuppyAgent
from code_puppy_core_plugins.file_permission_handler import folder_policy

GET = "code_puppy/approved_folders/get"
ADD = "code_puppy/approved_folders/add"
REMOVE = "code_puppy/approved_folders/remove"
CONFIG_KEY = "approved_folders"
CODE_PUPPY_EXT = [f"_{name}" for name in (GET, ADD, REMOVE)]


@pytest.fixture
def config(monkeypatch, tmp_path):
    """Route every config read and write to one in-memory store and record writes."""
    store: dict[str, Any] = {}
    writes: list[tuple[str, Any]] = []

    def fake_get_value(key: str) -> Any:
        return store.get(key)

    def fake_set_config_value(key: str, value: Any) -> None:
        writes.append((key, value))
        store[key] = value

    # folder_policy reads config through the module, so patching it covers both.
    monkeypatch.setattr("code_puppy.config.get_value", fake_get_value)
    monkeypatch.setattr("code_puppy.config.set_config_value", fake_set_config_value)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return SimpleNamespace(store=store, writes=writes, home=home)


@pytest.fixture
def agent(config):
    agent = CodePuppyAgent()
    yield agent
    io_delegation.uninstall()
    permissions.uninstall()


@pytest.fixture
def router(agent):
    return build_agent_router(agent)


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return path


def _real(path) -> str:
    return os.path.realpath(path)


def _assert_invalid_params(error: RequestError) -> None:
    assert error.code == -32602
    assert error.data["message"] == str(error)
    assert error.data["reason"] == str(error)


async def test_get_reports_the_four_policy_lists(router, project):
    result = await router(f"_{GET}", {}, False)

    assert result == {
        "projectFolder": _real(os.getcwd()),
        "tempFolders": folder_policy.always_approved_folders()["temp"],
        "userFolders": [],
        "protectedPaths": folder_policy.protected_paths(),
    }


async def test_add_then_remove_round_trips_through_the_config_fake(
    router, config, project
):
    added = await router(f"_{ADD}", {"path": str(project)}, False)

    assert added["userFolders"] == [_real(project)]
    assert config.writes == [(CONFIG_KEY, config.store[CONFIG_KEY])]

    removed = await router(f"_{REMOVE}", {"path": str(project)}, False)

    assert removed["userFolders"] == []
    assert len(config.writes) == 2


async def test_get_with_session_id_uses_that_sessions_cwd(router, agent, project):
    agent._sessions["sess_a"] = SimpleNamespace(cwd=str(project))

    result = await router(f"_{GET}", {"sessionId": "sess_a"}, False)

    assert result["projectFolder"] == _real(project)


async def test_get_without_session_id_uses_process_cwd(router, agent, project):
    agent._sessions["sess_a"] = SimpleNamespace(cwd=str(project))

    result = await router(f"_{GET}", {}, False)

    assert result["projectFolder"] == _real(os.getcwd())


async def test_unknown_session_id_is_invalid_params(router):
    with pytest.raises(RequestError) as caught:
        await router(f"_{GET}", {"sessionId": "sess_missing"}, False)

    _assert_invalid_params(caught.value)


@pytest.mark.parametrize("params", [{}, {"path": 3}, {"path": None}])
@pytest.mark.parametrize("method", [ADD, REMOVE], ids=["add", "remove"])
async def test_missing_or_non_string_path_is_invalid_params(router, method, params):
    with pytest.raises(RequestError) as caught:
        await router(f"_{method}", params, False)

    _assert_invalid_params(caught.value)


@pytest.mark.parametrize("case", ["relative", "missing", "root", "home"])
async def test_add_rejects_each_policy_error_case(router, config, tmp_path, case):
    paths = {
        "relative": "relative/dir",
        "missing": str(tmp_path / "does-not-exist"),
        "root": os.path.abspath(os.sep),
        "home": str(config.home),
    }

    with pytest.raises(RequestError) as caught:
        await router(f"_{ADD}", {"path": paths[case]}, False)

    _assert_invalid_params(caught.value)


async def test_remove_of_unlisted_folder_is_invalid_params(router, project):
    with pytest.raises(RequestError) as caught:
        await router(f"_{REMOVE}", {"path": str(project)}, False)

    _assert_invalid_params(caught.value)


async def test_unknown_extension_method_is_method_not_found(router):
    with pytest.raises(RequestError) as caught:
        await router("_code_puppy/approved_folders/nope", {}, False)

    assert caught.value.code == -32601
    assert caught.value.data == {"method": "_code_puppy/approved_folders/nope"}


async def test_initialize_advertises_extension_methods_in_meta(agent):
    response = await agent.initialize(protocol_version=1)

    dumped = response.model_dump(by_alias=True, exclude_none=True)
    meta = dumped["agentCapabilities"]["_meta"]
    assert meta == {
        "codePuppy": {"approvedFolders": {"methods": CODE_PUPPY_EXT}},
    }


async def test_initialize_keeps_existing_capability_fields(agent):
    response = await agent.initialize(protocol_version=1)

    capabilities = response.model_dump(by_alias=True, exclude_none=True)[
        "agentCapabilities"
    ]
    capabilities.pop("_meta")
    assert capabilities == {
        "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
        "loadSession": True,
        "sessionCapabilities": {
            "list": {},
            "close": {},
            "fork": {},
            "resume": {},
            "additionalDirectories": {},
        },
        # SDK defaults, never set by capabilities.py.
        "auth": {},
        "mcpCapabilities": {"acp": False, "http": False, "sse": False},
    }
