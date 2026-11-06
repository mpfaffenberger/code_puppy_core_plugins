"""Browser discovery and DevTools endpoint probing."""

from __future__ import annotations

import io
import json

import pytest

from code_puppy_core_plugins.browser_harness import browser


def test_only_drivable_browsers_are_recommended(monkeypatch):
    monkeypatch.setattr(browser.platform, "system", lambda: "Darwin")
    suggestions = browser.install_suggestions()

    assert "brew install --cask google-chrome" in suggestions
    assert all("firefox" not in s and "safari" not in s for s in suggestions)


@pytest.mark.parametrize(
    "system, expected",
    [
        ("Linux", "chromium"),
        ("Windows", "winget install -e --id Google.Chrome"),
    ],
)
def test_install_suggestions_follow_the_platform(system, expected, monkeypatch):
    monkeypatch.setattr(browser.platform, "system", lambda: system)
    assert any(expected in suggestion for suggestion in browser.install_suggestions())


def test_filters_split_browsers_by_drivability():
    found = [
        browser.Browser("Chrome", "/x", True, False),
        browser.Browser("Firefox", "/y", False, True),
    ]

    assert [item.name for item in browser.drivable_browsers(found)] == ["Chrome"]
    assert [item.name for item in browser.undrivable_browsers(found)] == ["Firefox"]


def test_mac_detection_trusts_the_filesystem(tmp_path, monkeypatch):
    apps = tmp_path / "Applications"
    (apps / "Firefox.app/Contents/MacOS/firefox").parent.mkdir(parents=True)
    (apps / "Firefox.app/Contents/MacOS/firefox").write_text("")
    chrome = apps / "Google Chrome.app/Contents/MacOS/Google Chrome"
    chrome.parent.mkdir(parents=True)
    chrome.write_text("")
    monkeypatch.setattr(browser.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser, "_MAC_APPLICATION_DIRS", (apps,))
    monkeypatch.setattr(browser, "_running_commands", lambda: {"google chrome"})

    found = browser.detect_browsers()

    assert {item.name for item in browser.drivable_browsers(found)} == {"Chrome"}
    assert [item.name for item in found if item.running] == ["Chrome"]
    assert [item.name for item in browser.undrivable_browsers(found)] == ["Firefox"]


def test_linux_detection_uses_path_lookup(tmp_path, monkeypatch):
    binaries = {"brave-browser": str(tmp_path / "brave-browser")}
    monkeypatch.setattr(browser.platform, "system", lambda: "Linux")
    monkeypatch.setattr(browser.shutil, "which", binaries.get)
    monkeypatch.setattr(browser, "_running_commands", lambda: {"brave-browser"})

    found = browser.detect_browsers()

    assert [item.name for item in found] == ["Brave"]
    assert found[0].running is True


def test_a_browser_that_is_not_running_stays_quiet(tmp_path, monkeypatch):
    apps = tmp_path / "Applications"
    chromium = apps / "Chromium.app/Contents/MacOS/Chromium"
    chromium.parent.mkdir(parents=True)
    chromium.write_text("")
    monkeypatch.setattr(browser.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser, "_MAC_APPLICATION_DIRS", (apps,))
    monkeypatch.setattr(browser, "_running_commands", lambda: set())

    assert browser.detect_browsers()[0].running is False


def test_running_process_probe_survives_a_missing_ps(monkeypatch):
    monkeypatch.setattr(
        browser.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no ps")),
    )
    assert browser._running_commands() == set()


def test_websocket_endpoints_are_reported_not_probed():
    endpoint = browser.probe_endpoint("wss://host.example/cdp")

    assert endpoint.reachable is None
    assert endpoint.product == "websocket endpoint"


def test_a_refused_port_is_unreachable():
    assert browser.probe_endpoint("http://127.0.0.1:1").reachable is False


def test_a_live_endpoint_names_itself(monkeypatch):
    payload = json.dumps({"Browser": "Chrome/144.0.0.0"}).encode()

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        browser.urllib.request, "urlopen", lambda url, timeout: Response(payload)
    )

    endpoint = browser.probe_endpoint("http://127.0.0.1:9222/")

    assert (endpoint.reachable, endpoint.product) == (True, "Chrome/144.0.0.0")


def test_reachable_endpoints_probe_the_defaults_plus_the_saved_url(monkeypatch):
    seen: list[str] = []

    def record(url):
        seen.append(url)
        return browser.Endpoint(url, reachable=False, product="unreachable")

    monkeypatch.setattr(browser, "probe_endpoint", record)

    browser.reachable_endpoints(["http://10.0.0.5:9222"])

    assert seen == [
        "http://127.0.0.1:9222",
        "http://127.0.0.1:9223",
        "http://10.0.0.5:9222",
    ]


def test_a_duplicated_endpoint_is_only_probed_once(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        browser,
        "probe_endpoint",
        lambda url: seen.append(url) or browser.Endpoint(url, False, "x"),
    )

    browser.reachable_endpoints(["http://127.0.0.1:9222", ""])

    assert seen == ["http://127.0.0.1:9222", "http://127.0.0.1:9223"]
