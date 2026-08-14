"""Regression coverage for the optional remote skills catalog."""

from __future__ import annotations

import importlib
import logging
from unittest.mock import Mock, patch

import httpx

import code_puppy_core_plugins.agent_skills.remote_catalog as rc


def _load_skill_catalog():
    """Reload the catalog module without triggering a lazy catalog fetch."""

    module = importlib.import_module(
        "code_puppy_core_plugins.agent_skills.skill_catalog"
    )
    return importlib.reload(module)


def test_catalog_fetch_is_lazy(monkeypatch) -> None:
    """Importing or constructing the catalog must not perform network I/O."""

    with patch.object(rc, "fetch_remote_catalog") as fetch_on_import:
        sc_module = _load_skill_catalog()

    fetch_on_import.assert_not_called()
    fetch_on_access = Mock(return_value=None)
    monkeypatch.setattr(sc_module, "fetch_remote_catalog", fetch_on_access)

    catalog = sc_module.SkillCatalog()
    fetch_on_access.assert_not_called()
    assert catalog.get_all() == []
    fetch_on_access.assert_called_once_with()
    assert catalog.list_categories() == []
    fetch_on_access.assert_called_once_with()


def test_transport_failure_is_debug_only(monkeypatch, caplog) -> None:
    """Expected offline transport failures must not emit startup-level noise."""

    class OfflineClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url: str):
            raise httpx.ConnectError("getaddrinfo failed")

    monkeypatch.setattr(rc.httpx, "Client", OfflineClient)
    with caplog.at_level(logging.DEBUG, logger=rc.__name__):
        assert rc._fetch_remote_json("https://example.test/skills.json") is None

    assert "getaddrinfo failed" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.INFO]


def test_no_cache_after_fetch_failure_is_debug_only(
    monkeypatch, tmp_path, caplog
) -> None:
    """An optional catalog miss with no cache must remain debug-only."""

    monkeypatch.setattr(rc, "_CACHE_PATH", tmp_path / "missing-catalog.json")
    monkeypatch.setattr(rc, "_fetch_remote_json", lambda url: None)

    with caplog.at_level(logging.DEBUG, logger=rc.__name__):
        assert rc.fetch_remote_catalog() is None

    assert "no cache is available" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.INFO]
