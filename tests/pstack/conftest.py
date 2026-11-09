"""pstack tests run with the pack switched on unless a test says otherwise.

The repo-wide conftest already points Code Puppy's config at a temp dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from code_puppy import config as cp_config

from code_puppy_core_plugins.pstack.config import set_enabled


@pytest.fixture(autouse=True)
def pstack_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cp_config, "AUTOSAVE_DIR", str(tmp_path / "autosaves"))
    set_enabled(True)
