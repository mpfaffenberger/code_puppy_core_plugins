"""Credential tests must exercise storage, never the developer's Keychain."""

import os
from pathlib import Path

import keyring
import pytest
from keyring.errors import PasswordDeleteError

from code_puppy import config, secret_store, shared_credentials
from tests.keyring_test_support import MemoryKeyring


@pytest.mark.parametrize("iteration", range(2))
def test_credentials_round_trip_with_fresh_storage(iteration):
    backend = keyring.get_keyring()
    assert os.environ.get("TYPESAFE_API_KEY") != "synthetic-keyring-test-value"
    assert isinstance(backend, MemoryKeyring)
    assert os.environ["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"
    secret_store._ensure_backend()
    assert keyring.get_keyring() is backend
    assert secret_store.keyring_available()
    assert shared_credentials.get("TYPESAFE_API_KEY") is None

    # Use the same real API as the popup-triggering settings test. No values
    # are logged; successful writes must really be readable and removable.
    config.set_config_value("owner_name", "Synthetic test owner")
    config.set_config_value("typesafe_api_key", "synthetic-keyring-test-value")
    assert shared_credentials.get("TYPESAFE_API_KEY") == "synthetic-keyring-test-value"
    assert "typesafe_api_key" not in Path(config.CONFIG_FILE).read_text()
    secret_store.delete_secret("provider_TYPESAFE_API_KEY")
    assert shared_credentials.get("TYPESAFE_API_KEY") is None
    assert not Path(secret_store._FALLBACK_FILE).exists()
    assert Path(secret_store._FALLBACK_FILE).parent == Path(config.CONFIG_DIR)
    assert Path(secret_store._FALLBACK_LOCK_FILE).parent == Path(config.CONFIG_DIR)


def test_memory_keyring_preserves_service_and_missing_entry_semantics():
    backend = keyring.get_keyring()
    backend.set_password("first", "account", "synthetic-first")
    backend.set_password("second", "account", "synthetic-second")
    assert backend.get_password("first", "account") == "synthetic-first"
    backend.delete_password("first", "account")
    assert backend.get_password("first", "account") is None
    assert backend.get_password("second", "account") == "synthetic-second"
    with pytest.raises(PasswordDeleteError):
        backend.delete_password("first", "account")
