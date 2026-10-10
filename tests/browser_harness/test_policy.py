"""Consent and connection settings for the browser-harness plugin."""

from __future__ import annotations

import json

import pytest

from code_puppy_core_plugins.browser_harness.policy import (
    BrowserHarnessError,
    SettingsStore,
)


def test_consent_starts_unset_and_blocks(store):
    assert store.is_enabled() is False
    assert store.consent_state() == "unset"
    with pytest.raises(BrowserHarnessError, match="one-time permission"):
        store.require_enabled()


def test_unset_consent_offers_the_enable_command(store):
    with pytest.raises(BrowserHarnessError, match=r"/browser enable"):
        store.require_enabled()


def test_declined_consent_gets_its_own_message(store):
    store.set_enabled(False)
    assert store.consent_state() == "disabled"
    assert store.is_enabled() is False
    with pytest.raises(BrowserHarnessError, match="disabled in settings"):
        store.require_enabled()


def test_opted_in_consent_persists(store):
    store.set_enabled(True)
    assert store.is_enabled() is True
    store.require_enabled()  # must not raise


def test_corrupt_settings_revert_to_asking_consent(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text("{not json")
    assert SettingsStore(path).consent_state() == "unset"


def test_values_of_the_wrong_type_are_ignored(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"enabled": "yes please", "endpoint": 42}))
    store = SettingsStore(path)
    assert store.consent_state() == "unset"
    assert store.endpoint() is None


def test_endpoint_must_be_a_devtools_url(store):
    with pytest.raises(BrowserHarnessError, match="Invalid CDP endpoint"):
        store.set_endpoint("localhost:9222")
    assert store.endpoint() is None


def test_endpoint_is_normalised_and_attributed(store):
    store.set_endpoint("  http://127.0.0.1:9222/  ")
    assert store.endpoint() == "http://127.0.0.1:9222"
    assert store.status()["endpoint_source"] == "saved"

    store.set_endpoint("wss://browser.example/devtools")
    assert store.endpoint() == "wss://browser.example/devtools"


def test_endpoint_defaults_to_auto_discovery(store):
    assert store.status()["endpoint_source"] == "auto-discovery"
    store.set_endpoint("http://127.0.0.1:9222")
    store.clear_endpoint()
    assert store.endpoint() is None
    assert store.status()["endpoint_source"] == "auto-discovery"
