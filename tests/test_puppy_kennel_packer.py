"""Packer length filter: explicit writes (P0/P1) bypass MIN_DRAWER_CHARS, P2 does not."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def kennel_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PUPPY_KENNEL_ROOT", str(tmp_path / "kennel"))
    monkeypatch.setenv("PUPPY_KENNEL_MIN_DRAWER_CHARS", "80")

    from code_puppy_core_plugins.puppy_kennel import config, kennel, packer

    importlib.reload(config)
    importlib.reload(kennel)
    importlib.reload(packer)
    kennel.initialize()
    repo = tmp_path / "repo"
    repo.mkdir()
    return kennel, packer, repo


def test_short_user_and_repo_notes_are_packed(kennel_env) -> None:
    kennel, packer, repo = kennel_env
    from code_puppy_core_plugins.puppy_kennel.wings import USER_WING, repo_wing

    kennel.write_note(USER_WING, "preferences", "User likes eating chicken.")
    kennel.write_note(repo_wing(repo), "notes", "Use uv, not pip.")

    block = packer.pack(cwd_override=str(repo))

    assert block is not None
    assert "User likes eating chicken." in block
    assert "Use uv, not pip." in block


def test_short_assistant_drawers_are_still_filtered(kennel_env) -> None:
    kennel, packer, repo = kennel_env
    from code_puppy_core_plugins.puppy_kennel.wings import repo_wing

    kennel.write_note(repo_wing(repo), "session-x", "ok", role="assistant")

    assert packer.pack(cwd_override=str(repo)) is None
