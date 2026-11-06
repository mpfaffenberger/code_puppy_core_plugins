"""detect_cwd follows the chat's working-directory override, not the process cwd."""

from __future__ import annotations

import os
from pathlib import Path

from code_puppy.tools.common import reset_working_directory, set_working_directory

from code_puppy_core_plugins.puppy_kennel.wings import detect_cwd


def test_override_wins_over_process_cwd(tmp_path: Path) -> None:
    chat_folder = tmp_path / "chat"
    chat_folder.mkdir()
    assert Path(os.getcwd()).resolve() != chat_folder.resolve()

    token = set_working_directory(str(chat_folder))
    try:
        assert detect_cwd().resolve() == chat_folder.resolve()
    finally:
        reset_working_directory(token)


def test_no_override_falls_back_to_process_cwd() -> None:
    assert detect_cwd() == Path(os.getcwd())


def test_sequential_scopes_each_return_their_own_folder(tmp_path: Path) -> None:
    folders = [tmp_path / "first", tmp_path / "second"]
    for folder in folders:
        folder.mkdir()

    for folder in folders:
        token = set_working_directory(str(folder))
        try:
            assert detect_cwd().resolve() == folder.resolve()
        finally:
            reset_working_directory(token)
