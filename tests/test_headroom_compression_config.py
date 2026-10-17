"""Tests for headroom_compression config accessors."""

from __future__ import annotations

from code_puppy_core_plugins.headroom_compression.config import (
    disable,
    enable,
    get_upstream_url,
    is_enabled,
)


def test_disabled_by_default():
    assert is_enabled() is False
    assert get_upstream_url() == ""


def test_enable_sets_flag_and_url():
    assert enable("https://example.com/anthropic") is True
    assert is_enabled() is True
    assert get_upstream_url() == "https://example.com/anthropic"


def test_enable_rejects_url_without_scheme():
    assert enable("example.com/anthropic") is False
    assert is_enabled() is False
    assert get_upstream_url() == ""


def test_enable_rejects_non_http_scheme():
    assert enable("ftp://example.com/anthropic") is False
    assert is_enabled() is False


def test_disable_clears_flag_but_keeps_url():
    enable("https://example.com/anthropic")
    disable()
    assert is_enabled() is False
    assert get_upstream_url() == "https://example.com/anthropic"
