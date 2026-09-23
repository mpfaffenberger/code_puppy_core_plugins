"""Tests for headroom_compression proxy lifecycle."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from code_puppy_core_plugins.headroom_compression import proxy as hp


def teardown_function(_fn):
    hp._proxy_process = None
    hp._proxy_active = False
    hp._upstream_url = ""


def test_headroom_bin_returns_none_when_not_installed():
    with (
        patch("shutil.which", return_value=None),
        patch("os.path.isfile", return_value=False),
    ):
        assert hp._headroom_bin() is None


def test_start_proxy_returns_false_when_no_binary():
    with patch.object(hp, "_headroom_bin", return_value=None):
        assert hp.start_proxy("https://example.com/anthropic") is False


def test_start_proxy_spawns_expected_argv():
    fake_proc = MagicMock(spec=subprocess.Popen)
    fake_proc.poll.return_value = None
    with (
        patch.object(hp, "_headroom_bin", return_value="/usr/local/bin/headroom"),
        patch("subprocess.Popen", return_value=fake_proc) as mock_popen,
        patch.object(hp, "_is_proxy_healthy", return_value=True),
    ):
        assert hp.start_proxy("https://example.com/anthropic") is True
    mock_popen.assert_called_once_with(
        [
            "/usr/local/bin/headroom",
            "proxy",
            "--port",
            "8787",
            "--anthropic-api-url",
            "https://example.com/anthropic",
            "--stateless",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_start_proxy_does_not_adopt_a_different_upstreams_process():
    # Simulate a proxy this instance already started for upstream A. A
    # request to enable upstream B must NOT short-circuit onto it, even
    # though it's healthy -- that would misroute B's traffic to A.
    hp._proxy_process = MagicMock(spec=subprocess.Popen)
    hp._proxy_process.poll.return_value = None
    hp._upstream_url = "https://a.example.com/anthropic"
    with (
        patch.object(hp, "_headroom_bin", return_value=None),
        patch.object(hp, "_is_proxy_healthy", return_value=True),
    ):
        # No binary available for the fresh spawn attempt this should fall
        # through to -- proves the healthy-but-wrong-upstream process was
        # not adopted.
        assert hp.start_proxy("https://b.example.com/anthropic") is False


def test_start_proxy_reuses_own_process_for_same_upstream():
    hp._proxy_process = MagicMock(spec=subprocess.Popen)
    hp._proxy_process.poll.return_value = None
    hp._upstream_url = "https://example.com/anthropic"
    with (
        patch.object(hp, "_headroom_bin", return_value="/bin/headroom"),
        patch("subprocess.Popen") as mock_popen,
        patch.object(hp, "_is_proxy_healthy", return_value=True),
    ):
        assert hp.start_proxy("https://example.com/anthropic") is True
    mock_popen.assert_not_called()


def test_start_proxy_kills_child_when_never_becomes_healthy():
    fake_proc = MagicMock(spec=subprocess.Popen)
    with (
        patch.object(hp, "_headroom_bin", return_value="/bin/headroom"),
        patch("subprocess.Popen", return_value=fake_proc),
        patch.object(hp, "_is_proxy_healthy", return_value=False),
        patch("time.sleep"),
    ):
        assert hp.start_proxy("https://example.com/anthropic") is False
    fake_proc.terminate.assert_called_once()
    assert hp._proxy_process is None


def test_stop_proxy_terminates_and_waits():
    fake_proc = MagicMock(spec=subprocess.Popen)
    hp._proxy_process = fake_proc
    hp._proxy_active = True
    hp.stop_proxy()
    fake_proc.terminate.assert_called_once()
    fake_proc.wait.assert_called_once()
    assert hp._proxy_process is None
    assert hp.is_active() is False


def test_stop_proxy_escalates_to_kill_on_timeout():
    fake_proc = MagicMock(spec=subprocess.Popen)
    fake_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="x", timeout=5), None]
    hp._proxy_process = fake_proc
    hp.stop_proxy()
    fake_proc.kill.assert_called_once()
    assert hp._proxy_process is None


def test_stop_proxy_is_safe_when_never_started():
    hp.stop_proxy()  # must not raise


def test_restart_proxy_falls_back_to_config_upstream(monkeypatch):
    from code_puppy_core_plugins.headroom_compression import config

    monkeypatch.setattr(config, "get_upstream_url", lambda: "https://cfg/anthropic")
    hp._upstream_url = ""
    with patch.object(hp, "start_proxy", return_value=True) as mock_start:
        assert hp.restart_proxy() is True
    mock_start.assert_called_once_with("https://cfg/anthropic")


def test_restart_proxy_noop_when_nothing_configured(monkeypatch):
    from code_puppy_core_plugins.headroom_compression import config

    monkeypatch.setattr(config, "get_upstream_url", lambda: "")
    hp._upstream_url = ""
    assert hp.restart_proxy() is False


def test_resolve_returns_none_when_inactive():
    hp._proxy_active = False
    assert hp.resolve_custom_endpoint_url("https://example.com/anthropic") is None


def test_resolve_rewrites_only_scheme_and_host_preserving_full_path():
    hp._proxy_active = True
    hp._upstream_url = "https://example.com/anthropic"
    assert (
        hp.resolve_custom_endpoint_url("https://example.com/anthropic/v1/messages")
        == "http://127.0.0.1:8787/anthropic/v1/messages"
    )


def test_resolve_leaves_other_hosts_untouched():
    hp._proxy_active = True
    hp._upstream_url = "https://example.com/anthropic"
    assert hp.resolve_custom_endpoint_url("https://api.z.ai/api/coding/paas/v4") is None


def test_resolve_leaves_sibling_path_on_shared_gateway_untouched():
    # The bug this guards: host-only matching would wrongly rewrite a
    # different API on the same shared gateway host.
    hp._proxy_active = True
    hp._upstream_url = "https://gateway.corp/anthropic"
    assert hp.resolve_custom_endpoint_url("https://gateway.corp/openai/v1/chat") is None


def test_resolve_leaves_mismatched_port_untouched():
    hp._proxy_active = True
    hp._upstream_url = "https://example.com/anthropic"
    assert (
        hp.resolve_custom_endpoint_url("https://example.com:9999/anthropic/v1/messages")
        is None
    )


def test_resolve_treats_default_and_explicit_port_as_equal():
    hp._proxy_active = True
    hp._upstream_url = "https://example.com/anthropic"
    assert (
        hp.resolve_custom_endpoint_url("https://example.com:443/anthropic/v1/messages")
        == "http://127.0.0.1:8787/anthropic/v1/messages"
    )


def test_resolve_leaves_mismatched_scheme_untouched():
    hp._proxy_active = True
    hp._upstream_url = "https://example.com/anthropic"
    assert (
        hp.resolve_custom_endpoint_url("http://example.com/anthropic/v1/messages")
        is None
    )


def test_resolve_returns_none_for_malformed_upstream():
    hp._proxy_active = True
    hp._upstream_url = "not-a-url"
    assert hp.resolve_custom_endpoint_url("not-a-url/v1/messages") is None


def test_check_and_fallback_noop_when_not_active():
    hp._proxy_active = False
    assert hp.check_and_fallback() is False


def test_check_and_fallback_deactivates_when_unhealthy():
    hp._proxy_active = True
    with patch.object(hp, "_is_proxy_healthy", return_value=False):
        assert hp.check_and_fallback() is True
    assert hp.is_active() is False


def test_check_and_fallback_noop_when_still_healthy():
    hp._proxy_active = True
    with patch.object(hp, "_is_proxy_healthy", return_value=True):
        assert hp.check_and_fallback() is False
    assert hp.is_active() is True


def test_on_agent_exception_ignores_unrelated_errors():
    hp._proxy_active = True
    hp.on_agent_exception_check_proxy(ValueError("bad input"))
    assert hp.is_active() is True


def test_on_agent_exception_falls_back_on_connection_error():
    hp._proxy_active = True
    with (
        patch.object(hp, "_is_proxy_healthy", return_value=False),
        patch.object(hp, "stop_proxy", wraps=hp.stop_proxy),
    ):
        hp.on_agent_exception_check_proxy(ConnectionError("refused"))
    assert hp.is_active() is False


def test_on_agent_exception_unwraps_wrapped_transport_error():
    class WrappedError(Exception):
        pass

    inner = TimeoutError("connect timed out")
    outer = WrappedError("agent failed")
    outer.__cause__ = inner

    hp._proxy_active = True
    with patch.object(hp, "_is_proxy_healthy", return_value=False):
        hp.on_agent_exception_check_proxy(outer)
    assert hp.is_active() is False


def test_looks_like_connection_failure_true_for_httpx_style_name():
    class ConnectError(Exception):
        __module__ = "httpx"

    assert hp._looks_like_connection_failure(ConnectError()) is True


def test_looks_like_connection_failure_false_for_unrelated_error():
    assert hp._looks_like_connection_failure(ValueError("nope")) is False
