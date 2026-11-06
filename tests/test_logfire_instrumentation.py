"""Tests for the logfire_instrumentation plugin."""

from __future__ import annotations

import os
import sys
import types
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from code_puppy_core_plugins.logfire_instrumentation import (
    instrumentation,
    legacy_core,
    sdk,
)
from code_puppy_core_plugins.logfire_instrumentation import (
    register_callbacks as callbacks,
)

_real_legacy_present = legacy_core.present


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(instrumentation, "_active", False)
    monkeypatch.setattr(instrumentation, "_agent_contexts", {})
    monkeypatch.setattr(legacy_core, "present", lambda: False)
    monkeypatch.setattr(legacy_core, "loaded", lambda: None)
    monkeypatch.delenv(instrumentation.ENABLE_ENV, raising=False)
    monkeypatch.delenv(legacy_core.TOKEN_ENV, raising=False)
    monkeypatch.setattr(
        "code_puppy_core_plugins.logfire_oauth.oauth.load_project_credentials",
        lambda: None,
    )


@pytest.fixture
def messages(monkeypatch):
    sink = Mock()
    monkeypatch.setattr("code_puppy.messaging.emit_warning", sink.warning)
    monkeypatch.setattr("code_puppy.messaging.emit_system_message", sink.system)
    return sink


@pytest.fixture
def opted_in(monkeypatch):
    monkeypatch.setenv(instrumentation.ENABLE_ENV, "1")


@pytest.fixture
def fake_logfire(monkeypatch):
    calls: dict[str, list] = {"configure": [], "instrument": [], "warning": []}
    attached: list = []

    @contextmanager
    def attach_context(context):
        attached.append(context)
        yield

    fake = types.SimpleNamespace(
        configure=lambda **kw: calls["configure"].append(kw),
        instrument_pydantic_ai=lambda: calls["instrument"].append(True),
        get_context=lambda: {"traceparent": "span"},
        attach_context=attach_context,
        warning=lambda msg, **kw: calls["warning"].append((msg, kw)),
        calls=calls,
        attached=attached,
    )
    monkeypatch.setitem(sys.modules, "logfire", fake)
    return fake


def _stored_token(monkeypatch, token: str) -> None:
    monkeypatch.setattr(
        "code_puppy_core_plugins.logfire_oauth.oauth.load_project_credentials",
        lambda: types.SimpleNamespace(token=token),
    )


# ------------------------------------------------------------------ opt-in
@pytest.mark.parametrize(
    ("env", "config", "expected"),
    [
        (None, None, False),
        (None, "true", True),
        (None, "off", False),
        ("1", None, True),
        (" YES ", "false", True),
        # A falsy env var does not force the opt-in off; config still decides.
        ("false", "true", True),
        ("false", None, False),
    ],
)
def test_opted_in(monkeypatch, env, config, expected):
    if env is not None:
        monkeypatch.setenv(instrumentation.ENABLE_ENV, env)
    monkeypatch.setattr("code_puppy.config.get_value", lambda key: config)
    assert instrumentation.opted_in() is expected


def test_dormant_configure_never_looks_for_the_sdk(monkeypatch, messages):
    monkeypatch.setitem(sys.modules, "logfire", None)
    monkeypatch.setattr(
        instrumentation, "sdk_available", Mock(side_effect=AssertionError)
    )
    legacy = Mock()
    monkeypatch.setattr(legacy_core, "loaded", lambda: legacy)

    assert instrumentation.configure() is False
    legacy.configure_logfire.assert_not_called()
    messages.warning.assert_not_called()
    assert legacy_core.TOKEN_ENV not in os.environ


# --------------------------------------------------------------- configure
def test_missing_sdk_warns_with_install_hint(monkeypatch, opted_in, messages):
    monkeypatch.setattr(instrumentation, "sdk_available", lambda: False)

    assert instrumentation.configure() is False
    messages.warning.assert_called_once_with(sdk.MISSING_SDK_HINT)
    assert instrumentation._active is False


def test_configure_passes_token_explicitly(
    monkeypatch, opted_in, fake_logfire, messages
):
    _stored_token(monkeypatch, "pylf-stored")

    assert instrumentation.configure() is True
    assert fake_logfire.calls["configure"] == [
        {
            "service_name": "code-puppy",
            "token": "pylf-stored",
            "send_to_logfire": "if-token-present",
            "console": False,
        }
    ]
    assert fake_logfire.calls["instrument"] == [True]
    assert instrumentation._active is True
    messages.system.assert_called_once()
    assert legacy_core.TOKEN_ENV not in os.environ


def test_user_env_token_beats_stored_token(monkeypatch):
    _stored_token(monkeypatch, "pylf-stored")
    monkeypatch.setenv(legacy_core.TOKEN_ENV, "pylf-user")
    assert instrumentation.resolve_token() == "pylf-user"


def test_no_token_anywhere_resolves_to_none():
    assert instrumentation.resolve_token() is None


def test_reconfigure_applies_the_current_token(
    monkeypatch, opted_in, fake_logfire, messages
):
    assert instrumentation.configure() is True
    _stored_token(monkeypatch, "pylf-minted")
    assert instrumentation.configure() is True

    assert [kw["token"] for kw in fake_logfire.calls["configure"]] == [
        None,
        "pylf-minted",
    ]


def test_explicit_token_beats_a_stale_env_token(
    monkeypatch, opted_in, fake_logfire, messages
):
    monkeypatch.setenv(legacy_core.TOKEN_ENV, "pylf-stale")

    assert instrumentation.configure(token="pylf-new") is True
    assert fake_logfire.calls["configure"][0]["token"] == "pylf-new"


def test_configure_failure_fails_soft_and_deactivates(
    monkeypatch, opted_in, fake_logfire, messages
):
    assert instrumentation.configure() is True

    def boom(**_kw):
        raise RuntimeError("bad config")

    monkeypatch.setattr(fake_logfire, "configure", boom)
    assert instrumentation.configure() is False
    assert instrumentation._active is False
    messages.warning.assert_called_once_with("Failed to configure Logfire: bad config")


# ------------------------------------------------------------ legacy core
def test_legacy_core_owns_configuration(monkeypatch, opted_in, fake_logfire, messages):
    legacy = Mock()
    legacy.configure_logfire.return_value = True
    monkeypatch.setattr(legacy_core, "present", lambda: True)
    monkeypatch.setattr(legacy_core, "loaded", lambda: legacy)
    _stored_token(monkeypatch, "pylf-minted")

    assert instrumentation.configure() is True
    legacy.configure_logfire.assert_called_once_with()
    assert os.environ[legacy_core.TOKEN_ENV] == "pylf-minted"
    assert fake_logfire.calls["configure"] == []
    assert instrumentation._active is False

    instrumentation.capture_context("g1")
    instrumentation.emit_cancellation("g1")
    assert fake_logfire.calls["warning"] == []


def test_legacy_result_is_mirrored(monkeypatch, opted_in):
    legacy = Mock()
    legacy.configure_logfire.return_value = False
    monkeypatch.setattr(legacy_core, "loaded", lambda: legacy)
    assert instrumentation.configure() is False


def test_legacy_core_receives_a_newly_minted_token(monkeypatch, opted_in):
    monkeypatch.setattr(legacy_core, "present", lambda: True)
    monkeypatch.setattr(legacy_core, "loaded", lambda: Mock())
    monkeypatch.setenv(legacy_core.TOKEN_ENV, "pylf-stale")

    instrumentation.configure(token="pylf-new")
    assert os.environ[legacy_core.TOKEN_ENV] == "pylf-new"


def test_export_token_is_noop_without_legacy_core_or_token(monkeypatch):
    legacy_core.export_token("pylf", overwrite=True)
    assert legacy_core.TOKEN_ENV not in os.environ

    monkeypatch.setattr(legacy_core, "present", lambda: True)
    legacy_core.export_token(None, overwrite=True)
    assert legacy_core.TOKEN_ENV not in os.environ


def test_export_token_respects_overwrite(monkeypatch):
    monkeypatch.setattr(legacy_core, "present", lambda: True)
    monkeypatch.setenv(legacy_core.TOKEN_ENV, "pylf-user")

    legacy_core.export_token("pylf-stored", overwrite=False)
    assert os.environ[legacy_core.TOKEN_ENV] == "pylf-user"
    legacy_core.export_token("pylf-minted", overwrite=True)
    assert os.environ[legacy_core.TOKEN_ENV] == "pylf-minted"


@pytest.mark.parametrize("error", [ValueError, ModuleNotFoundError, ImportError])
def test_discovery_never_raises(monkeypatch, error):
    monkeypatch.delitem(sys.modules, "logfire", raising=False)
    monkeypatch.setattr(
        "importlib.util.find_spec", Mock(side_effect=error("broken spec"))
    )
    assert _real_legacy_present() is False
    assert sdk.sdk_available() is False


def test_sdk_blocked_in_sys_modules_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "logfire", None)
    assert sdk.sdk_available() is False


# ------------------------------------------------- context and cancellation
def test_hooks_are_inert_until_active(fake_logfire):
    instrumentation.capture_context("g1")
    instrumentation.emit_cancellation("g1")
    assert instrumentation._agent_contexts == {}
    assert fake_logfire.calls["warning"] == []


def test_cancellation_nests_under_captured_span(monkeypatch, fake_logfire):
    monkeypatch.setattr(instrumentation, "_active", True)
    instrumentation.capture_context("g1")

    instrumentation.emit_cancellation("g1")
    assert fake_logfire.attached == [{"traceparent": "span"}]
    assert fake_logfire.calls["warning"] == [
        ("Agent run cancelled", {"group_id": "g1"})
    ]
    assert "g1" not in instrumentation._agent_contexts


def test_cancellation_without_context_still_logs(monkeypatch, fake_logfire):
    monkeypatch.setattr(instrumentation, "_active", True)
    instrumentation.emit_cancellation("g2")
    assert fake_logfire.attached == []
    assert fake_logfire.calls["warning"] == [
        ("Agent run cancelled", {"group_id": "g2"})
    ]


def test_capture_overwrites_with_latest_span(monkeypatch, fake_logfire):
    monkeypatch.setattr(instrumentation, "_active", True)
    instrumentation.capture_context("g1")
    monkeypatch.setattr(fake_logfire, "get_context", lambda: {"traceparent": "new"})
    instrumentation.capture_context("g1")
    assert instrumentation._agent_contexts == {"g1": {"traceparent": "new"}}


def test_clear_context_works_even_when_inactive(monkeypatch):
    monkeypatch.setattr(instrumentation, "_agent_contexts", {"g1": {}})
    instrumentation.clear_context("g1")
    instrumentation.clear_context("missing")
    assert instrumentation._agent_contexts == {}


def test_sdk_errors_never_escape(monkeypatch, fake_logfire):
    monkeypatch.setattr(instrumentation, "_active", True)

    def boom(*_args, **_kwargs):
        raise RuntimeError("sdk exploded")

    monkeypatch.setattr(fake_logfire, "get_context", boom)
    monkeypatch.setattr(fake_logfire, "warning", boom)
    instrumentation.capture_context("g1")
    instrumentation.emit_cancellation("g1")


# ------------------------------------------------------- register_callbacks
def test_startup_configures_on_new_core(monkeypatch):
    configure = Mock()
    monkeypatch.setattr(instrumentation, "configure", configure)
    callbacks._on_startup()
    configure.assert_called_once_with()


def test_startup_defers_to_legacy_core(monkeypatch):
    configure = Mock()
    monkeypatch.setattr(instrumentation, "configure", configure)
    monkeypatch.setattr(legacy_core, "present", lambda: True)
    callbacks._on_startup()
    configure.assert_not_called()


def test_agent_run_end_clears_context(monkeypatch):
    monkeypatch.setattr(instrumentation, "_agent_contexts", {"g1": {}})
    callbacks._on_agent_run_end("agent", "model", "g1", True, None, "done", {})
    callbacks._on_agent_run_end("agent", "model", None)
    assert instrumentation._agent_contexts == {}


def test_event_stream_start_registration(monkeypatch):
    register = Mock()
    monkeypatch.setattr(callbacks, "register_callback", register)
    callbacks._register_event_stream_start()
    register.assert_called_once_with(
        "agent_event_stream_start", instrumentation.capture_context
    )


def test_event_stream_start_tolerates_older_core(monkeypatch):
    monkeypatch.setattr(
        callbacks, "register_callback", Mock(side_effect=ValueError("Unsupported"))
    )
    callbacks._register_event_stream_start()


def test_legacy_token_export_requires_opt_in(monkeypatch):
    _stored_token(monkeypatch, "pylf-stored")
    monkeypatch.setattr(legacy_core, "present", lambda: True)

    callbacks._export_token_for_legacy_core()
    assert legacy_core.TOKEN_ENV not in os.environ

    monkeypatch.setenv(instrumentation.ENABLE_ENV, "true")
    callbacks._export_token_for_legacy_core()
    assert os.environ[legacy_core.TOKEN_ENV] == "pylf-stored"


def test_no_env_export_on_new_core(monkeypatch, opted_in):
    _stored_token(monkeypatch, "pylf-stored")
    callbacks._export_token_for_legacy_core()
    assert legacy_core.TOKEN_ENV not in os.environ


def test_legacy_token_export_never_raises(monkeypatch, opted_in):
    monkeypatch.setattr(legacy_core, "present", lambda: True)
    monkeypatch.setattr(
        instrumentation, "resolve_token", Mock(side_effect=OSError("unreadable"))
    )
    callbacks._export_token_for_legacy_core()
