"""Shared fixtures for the ACP auth-status tests."""

from __future__ import annotations

import pytest

from code_puppy_core_plugins.acp import auth_status


@pytest.fixture
def clean_registry(monkeypatch):
    """Isolate the module-level provider registry for one test."""
    monkeypatch.setattr(auth_status, "_providers", [])
