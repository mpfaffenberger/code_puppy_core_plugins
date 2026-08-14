"""Regression coverage for the optional remote skills catalog."""

from __future__ import annotations

import importlib
import logging
import threading
from unittest.mock import Mock, patch

import httpx

import code_puppy_core_plugins.agent_skills.remote_catalog as rc
from code_puppy_core_plugins.agent_skills.remote_catalog import (
    RemoteCatalogData,
    RemoteSkillEntry,
)


def _load_skill_catalog():
    """Reload the catalog module without triggering a lazy catalog fetch."""

    module = importlib.import_module(
        "code_puppy_core_plugins.agent_skills.skill_catalog"
    )
    return importlib.reload(module)


def _remote_catalog() -> RemoteCatalogData:
    """Build a minimal valid catalog response."""

    entry = RemoteSkillEntry(
        name="example-skill",
        description="Example",
        group="testing",
        download_url="https://example.test/example.zip",
        zip_size_bytes=1,
        file_count=1,
        has_scripts=False,
        has_references=False,
        has_license=False,
    )
    return RemoteCatalogData("1", "https://example.test", 1, [], [entry])


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


def test_catalog_retries_after_failure_backoff(monkeypatch) -> None:
    """A transient failure retries after the bounded backoff expires."""

    sc_module = _load_skill_catalog()
    fetch = Mock(side_effect=[None, _remote_catalog()])
    monotonic_values = iter([0, 0, 0, 59, 60, 60])
    monkeypatch.setattr(sc_module, "fetch_remote_catalog", fetch)
    monkeypatch.setattr(sc_module.time, "monotonic", lambda: next(monotonic_values))

    catalog = sc_module.SkillCatalog()
    assert catalog.get_all() == []
    assert catalog.get_all() == []
    assert [entry.id for entry in catalog.get_all()] == ["example-skill"]
    assert fetch.call_count == 2


def test_catalog_initialization_is_serialized(monkeypatch) -> None:
    """Concurrent readers wait for one complete catalog initialization."""

    sc_module = _load_skill_catalog()
    started = threading.Event()
    release = threading.Event()
    fetch = Mock()

    def blocking_fetch():
        started.set()
        assert release.wait(timeout=2)
        return _remote_catalog()

    fetch.side_effect = blocking_fetch
    monkeypatch.setattr(sc_module, "fetch_remote_catalog", fetch)
    catalog = sc_module.SkillCatalog()
    results: list[list[str]] = []

    def read_catalog():
        results.append([entry.id for entry in catalog.get_all()])

    first = threading.Thread(target=read_catalog)
    second = threading.Thread(target=read_catalog)
    first.start()
    assert started.wait(timeout=2)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert fetch.call_count == 1
    assert results == [["example-skill"], ["example-skill"]]


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
