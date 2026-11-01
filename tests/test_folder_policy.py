"""Tests for the folder approval policy.

Config is patched onto an in-memory dict; temp and home roots are redirected
under tmp_path so no test reads or writes the real puppy.cfg.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

import code_puppy.tools.common as common
from code_puppy_core_plugins.file_permission_handler import folder_policy
from code_puppy_core_plugins.file_permission_handler.folder_policy import (
    Decision,
    PolicyError,
)

ASK = Decision.ASK
ALLOW = Decision.ALLOW
DEFAULT = Decision.DEFAULT

PROTECTED_FILE_NAMES = [
    ".env",
    ".env.local",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "id_rsa",
    "id_rsa.pub",
    "id_ed25519",
    "server.pem",
    "tls.key",
    "cert.p12",
    "cert.pfx",
    "ca.crt",
    "ca.cer",
    "store.keystore",
    "store.jks",
]
PROTECTED_RELATIVE_PATHS = [
    ".git/config",
    ".git/worktrees/x/file",
    ".agents/skills/a.md",
    ".codex/config.toml",
    ".aws/credentials",
    ".code_puppy/puppy.cfg",
    ".ssh/config",
    ".gnupg/pubring.kbx",
]
NOT_PROTECTED_RELATIVE_PATHS = [
    ".gitignore",
    ".github",
    ".github/workflows/ci.yml",
    "src/app.py",
]
CORRUPT_CONFIG_VALUES = [
    "{not json",
    '{"path": "/x"}',
    '["ok", 3]',
    '"/just/a/string"',
    '["relative/dir"]',
    "null",
    "3",
]


@pytest.fixture
def config_store(monkeypatch):
    values: dict[str, str] = {}
    monkeypatch.setattr(folder_policy.config, "get_value", values.get)
    monkeypatch.setattr(folder_policy.config, "set_config_value", values.__setitem__)
    return values


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path, config_store):
    tmp_dir = tmp_path / "tmpdir"
    tmp_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(tmp_dir))
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_dir))
    # Keeps the real /tmp out of "outside" tests; tests that need it patch it back.
    monkeypatch.setattr(folder_policy, "_SYSTEM_TMP", str(tmp_path / "no-system-tmp"))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def outside(tmp_path):
    root = tmp_path / "outside"
    root.mkdir()
    return root


def decide(path, cwd, is_delete=False):
    return folder_policy.decide(str(path), is_delete=is_delete, cwd=str(cwd))


@pytest.mark.parametrize("name", PROTECTED_FILE_NAMES)
@pytest.mark.parametrize("is_delete", [False, True])
def test_protected_file_names_always_ask(project, name, is_delete):
    assert decide(project / name, project, is_delete) is ASK


@pytest.mark.parametrize("relative", PROTECTED_RELATIVE_PATHS)
@pytest.mark.parametrize("is_delete", [False, True])
def test_protected_path_components_always_ask(project, relative, is_delete):
    assert folder_policy.is_protected(str(project / relative))
    assert decide(project / relative, project, is_delete) is ASK


@pytest.mark.parametrize("relative", NOT_PROTECTED_RELATIVE_PATHS)
def test_gitignore_and_github_are_not_protected(project, relative):
    assert not folder_policy.is_protected(str(project / relative))
    assert decide(project / relative, project) is ALLOW


def test_protected_inside_user_folder_still_asks(project, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    folder_policy.add_user_folder(str(shared))
    assert decide(shared / ".ssh" / "id", project) is ASK
    assert decide(shared / ".env", project, is_delete=True) is ASK


def test_protected_inside_literal_tmp_still_asks(project, monkeypatch):
    monkeypatch.setattr(folder_policy, "_SYSTEM_TMP", "/tmp")
    assert decide("/tmp/app/.env", project) is ASK
    assert decide("/tmp/app/.git/config", project, is_delete=True) is ASK


def test_literal_tmp_is_approved(project, monkeypatch):
    monkeypatch.setattr(folder_policy, "_SYSTEM_TMP", "/tmp")
    assert decide("/tmp/acp-probe/out.txt", project) is ALLOW


@pytest.mark.parametrize(
    "platform, expects_system_tmp",
    [("linux", True), ("darwin", True), ("win32", False)],
)
def test_system_tmp_is_a_temp_root_only_off_windows(
    tmp_path, monkeypatch, platform, expects_system_tmp
):
    system_tmp = tmp_path / "system-tmp"
    monkeypatch.setattr(folder_policy, "_SYSTEM_TMP", str(system_tmp))
    monkeypatch.setattr(folder_policy.sys, "platform", platform)
    expected = os.path.realpath(system_tmp)
    assert (expected in folder_policy._temp_roots()) is expects_system_tmp


def test_tmpdir_env_is_approved(project):
    scratch = Path(os.environ["TMPDIR"]) / "scratch.txt"
    assert decide(scratch, project) is ALLOW
    assert decide(scratch, project, is_delete=True) is ALLOW


@pytest.mark.parametrize("var", ["TEMP", "TMP"])
def test_temp_env_vars_are_approved(project, tmp_path, monkeypatch, var):
    temp_root = tmp_path / var.lower()
    monkeypatch.setenv(var, str(temp_root))
    assert decide(temp_root / "a.txt", project) is ALLOW


def test_symlinked_tmpdir_resolves_to_real_target(project, tmp_path, monkeypatch):
    link = tmp_path / "tmp-link"
    link.symlink_to(os.environ["TMPDIR"], target_is_directory=True)
    monkeypatch.setenv("TMPDIR", str(link))
    assert decide(Path(os.environ["TMPDIR"]) / "a.txt", project) is ALLOW
    assert decide(link / "a.txt", project) is ALLOW


def test_project_cwd_is_approved(project):
    assert decide(project / "src" / "a.py", project) is ALLOW
    assert decide("src/a.py", project) is ALLOW
    assert decide(project / "src" / "a.py", project, is_delete=True) is ALLOW


def test_cwd_none_uses_session_working_directory(project, monkeypatch):
    monkeypatch.setattr(common, "get_working_directory", lambda: str(project))
    assert folder_policy.decide("a.py") is ALLOW


def test_cwd_none_falls_back_to_getcwd_without_code_puppy(project, monkeypatch):
    monkeypatch.setitem(sys.modules, "code_puppy.tools.common", None)
    monkeypatch.chdir(project)
    assert folder_policy.decide("a.py") is ALLOW


def test_outside_edit_is_default_and_delete_asks(project, outside):
    target = outside / "a.txt"
    assert decide(target, project) is DEFAULT
    assert decide(target, project, is_delete=True) is ASK
    assert not folder_policy.is_approved(str(target), str(project))


@pytest.mark.parametrize("is_delete, expected", [(False, DEFAULT), (True, ASK)])
@pytest.mark.parametrize("escape", ["{project}/../outside/a.txt", "../outside/a.txt"])
def test_dotdot_traversal_out_of_project_is_not_approved(
    project, outside, escape, is_delete, expected
):
    path = escape.format(project=project)
    assert decide(path, project, is_delete) is expected


def test_symlinked_directory_escape_is_not_approved(project, outside):
    link = project / "escape"
    link.symlink_to(outside, target_is_directory=True)
    assert decide(link / "a.txt", project) is DEFAULT
    assert decide(link / "a.txt", project, is_delete=True) is ASK


def test_symlink_named_like_secret_asks_even_when_target_is_plain(project, outside):
    link = project / "creds.pem"
    link.symlink_to(outside / "data.txt")
    assert decide(link, project) is ASK


def test_sibling_named_like_approved_dir_is_not_inside(project, tmp_path):
    sibling = tmp_path / "project-evil"
    sibling.mkdir()
    assert decide(sibling / "a.txt", project) is DEFAULT
    assert not folder_policy.is_approved(str(sibling / "a.txt"), str(project))


def test_user_folder_sibling_named_like_it_is_not_inside(project, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (tmp_path / "shared-evil").mkdir()
    folder_policy.add_user_folder(str(shared))
    assert decide(tmp_path / "shared-evil" / "a.txt", project) is DEFAULT


@pytest.mark.parametrize(
    "platform, expected_equal",
    [("darwin", True), ("win32", True), ("linux", False)],
)
def test_path_comparison_is_case_insensitive_only_on_mac_and_windows(
    monkeypatch, platform, expected_equal
):
    monkeypatch.setattr(folder_policy.sys, "platform", platform)
    assert (folder_policy._fold("/Tmp/x") == folder_policy._fold("/tmp/x")) is (
        expected_equal
    )
    assert folder_policy._is_within("/Tmp/X/a.txt", "/tmp/x") is expected_equal


@pytest.mark.parametrize(
    "platform, expected_protected",
    [("darwin", True), ("linux", False)],
)
def test_protected_name_match_follows_platform_case_rule(
    monkeypatch, platform, expected_protected
):
    monkeypatch.setattr(folder_policy.sys, "platform", platform)
    assert folder_policy.is_protected("/proj/.ENV") is expected_protected


def test_add_user_folder_persists_normalized_path(project, config_store):
    result = folder_policy.add_user_folder(str(project))
    assert result == [os.path.realpath(project)]
    assert json.loads(config_store["approved_folders"]) == result
    assert folder_policy.get_user_folders() == result


def test_add_user_folder_is_idempotent_across_symlink_aliases(
    project, tmp_path, config_store
):
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    first = folder_policy.add_user_folder(str(project))
    second = folder_policy.add_user_folder(str(project))
    third = folder_policy.add_user_folder(str(alias))
    assert first == second == third
    assert len(json.loads(config_store["approved_folders"])) == 1


@pytest.mark.parametrize("kind", ["relative", "missing", "file", "root", "home"])
def test_add_user_folder_rejects_invalid_targets(tmp_path, config_store, kind):
    if kind == "relative":
        bad = "relative/dir"
    elif kind == "missing":
        bad = str(tmp_path / "nope")
    elif kind == "file":
        file_path = tmp_path / "f.txt"
        file_path.write_text("x")
        bad = str(file_path)
    elif kind == "root":
        bad = Path(Path.home().anchor).as_posix()
    else:
        bad = str(Path.home())
    with pytest.raises(PolicyError):
        folder_policy.add_user_folder(bad)
    assert folder_policy.get_user_folders() == []
    assert "approved_folders" not in config_store


def test_remove_user_folder_updates_list(project, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    folder_policy.add_user_folder(str(project))
    folder_policy.add_user_folder(str(shared))
    remaining = folder_policy.remove_user_folder(str(project))
    assert remaining == [os.path.realpath(shared)]
    assert folder_policy.get_user_folders() == remaining


def test_remove_missing_folder_raises(project):
    with pytest.raises(PolicyError):
        folder_policy.remove_user_folder(str(project))


def test_missing_config_reads_as_empty():
    assert folder_policy.get_user_folders() == []


@pytest.mark.parametrize("raw", CORRUPT_CONFIG_VALUES)
def test_corrupt_config_reads_as_empty(config_store, raw):
    config_store["approved_folders"] = raw
    assert folder_policy.get_user_folders() == []


def test_corrupt_config_is_overwritten_by_next_add(project, config_store):
    config_store["approved_folders"] = "{not json"
    assert folder_policy.add_user_folder(str(project)) == [os.path.realpath(project)]


def test_stored_duplicates_are_deduped_on_read(project, config_store):
    path = os.path.realpath(project)
    config_store["approved_folders"] = json.dumps([path, path])
    assert folder_policy.get_user_folders() == [path]


def test_user_folder_allows_edit_and_delete(project, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    folder_policy.add_user_folder(str(shared))
    assert decide(shared / "a.txt", project) is ALLOW
    assert decide(shared / "a.txt", project, is_delete=True) is ALLOW


@pytest.mark.parametrize("relative", [".env", ".git/config", "id_rsa", "keys/x.pem"])
def test_user_folder_does_not_bypass_protected(project, tmp_path, relative):
    shared = tmp_path / "shared"
    shared.mkdir()
    folder_policy.add_user_folder(str(shared))
    assert decide(shared / relative, project) is ASK


def test_always_approved_folders_shape_and_dedupe(project, monkeypatch):
    monkeypatch.setenv("TEMP", os.environ["TMPDIR"])
    result = folder_policy.always_approved_folders(str(project))
    assert set(result) == {"project", "temp"}
    assert result["project"] == os.path.realpath(project)
    temp = result["temp"]
    assert len(temp) == len(set(temp))
    assert os.path.realpath(os.environ["TMPDIR"]) in temp


def test_protected_paths_display_form():
    shown = set(folder_policy.protected_paths())
    expected = {
        ".git/",
        ".agents/",
        ".codex/",
        ".aws/",
        ".code_puppy/",
        ".ssh/",
        ".gnupg/",
        ".env",
        ".env.*",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "id_rsa*",
        "id_ed25519*",
        "*.pem",
        "*.key",
        "*.p12",
        "*.pfx",
        "*.crt",
        "*.cer",
        "*.keystore",
        "*.jks",
    }
    assert expected <= shown
    assert ".gitignore" not in shown
    assert ".github" not in shown


def test_resolve_collapses_dotdot_for_missing_file(project):
    resolved = folder_policy.resolve(str(project / "new" / ".." / "a.txt"))
    assert resolved == os.path.realpath(project / "a.txt")


def test_resolve_expands_home(tmp_path):
    assert folder_policy.resolve("~/x") == os.path.realpath(tmp_path / "home" / "x")
