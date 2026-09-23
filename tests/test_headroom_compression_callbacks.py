"""Tests confirming headroom_compression wires its callbacks into core."""

from __future__ import annotations

from code_puppy.callbacks import get_callbacks
from code_puppy_core_plugins.headroom_compression import (
    proxy,
    register_callbacks as rc,
)
from code_puppy_core_plugins.headroom_compression.command import (
    get_headroom_command_help,
    handle_headroom_command,
)


def test_startup_registered():
    assert rc._start_if_enabled in get_callbacks("startup")


def test_custom_command_registered():
    assert handle_headroom_command in get_callbacks("custom_command")


def test_custom_command_help_registered():
    assert get_headroom_command_help in get_callbacks("custom_command_help")


def test_resolve_custom_endpoint_url_registered():
    assert proxy.resolve_custom_endpoint_url in get_callbacks(
        "resolve_custom_endpoint_url"
    )


def test_agent_exception_registered():
    assert proxy.on_agent_exception_check_proxy in get_callbacks("agent_exception")


async def test_start_if_enabled_noop_when_disabled(monkeypatch):
    from code_puppy_core_plugins.headroom_compression import config

    monkeypatch.setattr(config, "is_enabled", lambda: False)
    called = []
    monkeypatch.setattr(proxy, "start_proxy", lambda url: called.append(url))
    await rc._start_if_enabled()
    assert called == []


async def test_start_if_enabled_starts_proxy_with_configured_url(monkeypatch):
    from code_puppy_core_plugins.headroom_compression import config

    monkeypatch.setattr(config, "is_enabled", lambda: True)
    monkeypatch.setattr(config, "get_upstream_url", lambda: "https://real/anthropic")
    called = []
    monkeypatch.setattr(proxy, "start_proxy", lambda url: called.append(url))
    await rc._start_if_enabled()
    assert called == ["https://real/anthropic"]


def test_end_to_end_through_the_core_hook(monkeypatch):
    """The plugin's entire value rests on this contract: once wired via
    register_callback, code_puppy.callbacks.on_resolve_custom_endpoint_url
    (the real core trigger, not a mock) must actually redirect through it."""
    from code_puppy.callbacks import on_resolve_custom_endpoint_url

    monkeypatch.setattr(proxy, "_proxy_active", True)
    monkeypatch.setattr(proxy, "_upstream_url", "https://example.com/anthropic")
    result = on_resolve_custom_endpoint_url("https://example.com/anthropic/v1/messages")
    assert result == "http://127.0.0.1:8787/anthropic/v1/messages"
