"""Approved-folder persistence through the CLI's real ``puppy.cfg`` (no config fake)."""

from __future__ import annotations

import configparser
import json
import os

import pytest
from acp import RequestError

from code_puppy import config
from code_puppy_core_plugins.acp import folders
from code_puppy_core_plugins.file_permission_handler import folder_policy

ADD = folders.ADD_METHOD
GET = folders.GET_METHOD
REMOVE = folders.REMOVE_METHOD
CONFIG_KEY = "approved_folders"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    path = tmp_path / "puppy.cfg"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", str(path))
    return path


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return path


def _restart(monkeypatch) -> None:
    monkeypatch.setattr(config, "_CONFIG_CACHE", None)


def _stored_folders(path) -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    return json.loads(parser[config.DEFAULT_SECTION][CONFIG_KEY])


def test_add_user_folder_is_written_to_puppy_cfg_and_survives_restart(
    cfg, project, monkeypatch
):
    real = os.path.realpath(project)

    folder_policy.add_user_folder(str(project))

    raw = cfg.read_text(encoding="utf-8")
    assert CONFIG_KEY in raw
    assert real in raw
    assert _stored_folders(cfg) == [real]
    assert folder_policy.get_user_folders() == [real]

    _restart(monkeypatch)

    assert folder_policy.get_user_folders() == [real]


def test_acp_add_get_remove_write_through_puppy_cfg(cfg, project):
    real = os.path.realpath(project)

    added = folders.handle(ADD, {"path": str(project)}, {})

    assert added["userFolders"] == [real]
    assert _stored_folders(cfg) == [real]

    fetched = folders.handle(GET, {}, {})

    assert fetched["userFolders"] == [real]

    removed = folders.handle(REMOVE, {"path": str(project)}, {})

    assert removed["userFolders"] == []
    assert real not in cfg.read_text(encoding="utf-8")
    assert _stored_folders(cfg) == []


@pytest.mark.parametrize("case", ["relative", "missing", "root", "home"])
def test_rejected_add_leaves_puppy_cfg_byte_identical(cfg, tmp_path, case):
    seeded = b"[puppy]\nmodel = x\n"
    cfg.write_bytes(seeded)
    paths = {
        "relative": "relative/dir",
        "missing": str(tmp_path / "does-not-exist"),
        "root": os.path.abspath(os.sep),
        "home": str(tmp_path / "home"),
    }

    with pytest.raises(RequestError) as caught:
        folders.handle(ADD, {"path": paths[case]}, {})

    assert caught.value.code == -32602
    assert cfg.read_bytes() == seeded


def test_unrelated_puppy_cfg_keys_survive_add_and_remove(cfg, project):
    cfg.write_text("[puppy]\nmodel = x\n", encoding="utf-8")

    folders.handle(ADD, {"path": str(project)}, {})

    assert "model = x" in cfg.read_text(encoding="utf-8")
    assert config.get_value("model") == "x"

    folders.handle(REMOVE, {"path": str(project)}, {})

    assert config.get_value("model") == "x"
    assert _stored_folders(cfg) == []
