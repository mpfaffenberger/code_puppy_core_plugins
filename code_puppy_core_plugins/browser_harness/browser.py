"""Discover local browsers, running state, and reachable CDP endpoints.

browser-harness drives browsers over the Chrome DevTools Protocol, so only the
Chromium family is drivable. Firefox and Safari do not expose a CDP endpoint;
they are reported as present-but-undrivable instead of being silently ignored.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

#: Ports browser-harness probes for a local DevTools endpoint.
DEFAULT_CDP_PORTS = (9222, 9223)
_PROBE_TIMEOUT_SECONDS = 0.4
_MAC_APPLICATION_DIRS = (Path("/Applications"), Path.home() / "Applications")
_WINDOWS_ROOTS = tuple(
    Path(root)
    for root in (
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
    )
    if root
)

UNSUPPORTED_NOTE = (
    "browser-harness speaks the Chrome DevTools Protocol. Firefox and Safari do "
    "not expose one, so they cannot be driven. Any Chromium-family browser "
    "(Chrome, Chromium, Brave, Edge, Arc, Helium) can be."
)


@dataclass(frozen=True)
class _Spec:
    """Where one browser lives on each platform, plus the names it runs as."""

    name: str
    drivable: bool
    mac: str
    linux: tuple[str, ...] = ()
    windows: str | None = None
    brew: str | None = None

    def process_names(self) -> set[str]:
        """Executable names this browser shows up as in a process list."""
        names = {Path(self.mac).name.casefold()}
        names.update(Path(binary).name.casefold() for binary in self.linux)
        if self.windows:
            names.add(Path(self.windows.replace("\\", "/")).name.casefold())
        return names


_SPECS: tuple[_Spec, ...] = (
    _Spec(
        "Chrome",
        True,
        "Google Chrome.app/Contents/MacOS/Google Chrome",
        ("google-chrome", "google-chrome-stable"),
        "Google\\Chrome\\Application\\chrome.exe",
        "google-chrome",
    ),
    _Spec(
        "Chrome Canary",
        True,
        "Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    ),
    _Spec(
        "Chromium",
        True,
        "Chromium.app/Contents/MacOS/Chromium",
        ("chromium", "chromium-browser"),
        "Chromium\\Application\\chrome.exe",
        "chromium",
    ),
    _Spec(
        "Brave",
        True,
        "Brave Browser.app/Contents/MacOS/Brave Browser",
        ("brave-browser",),
        "BraveSoftware\\Brave-Browser\\Application\\brave.exe",
        "brave-browser",
    ),
    _Spec(
        "Edge",
        True,
        "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ("microsoft-edge",),
        "Microsoft\\Edge\\Application\\msedge.exe",
        "microsoft-edge",
    ),
    _Spec("Arc", True, "Arc.app/Contents/MacOS/Arc"),
    _Spec("Helium", True, "Helium.app/Contents/MacOS/Helium"),
    _Spec(
        "Firefox",
        False,
        "Firefox.app/Contents/MacOS/firefox",
        ("firefox",),
        "Mozilla Firefox\\firefox.exe",
    ),
    _Spec("Safari", False, "Safari.app/Contents/MacOS/Safari"),
)


@dataclass(frozen=True)
class Browser:
    name: str
    path: str
    drivable: bool
    running: bool


@dataclass(frozen=True)
class Endpoint:
    url: str
    reachable: bool | None  # None means "not probeable" (raw WebSocket URL)
    product: str


def detect_browsers() -> list[Browser]:
    """Return every known browser installed on this machine."""
    system = platform.system()
    commands = _running_commands()
    found: list[Browser] = []
    for spec in _SPECS:
        path = _spec_path(spec, system)
        if path is None:
            continue
        found.append(
            Browser(
                name=spec.name,
                path=str(path),
                drivable=spec.drivable,
                running=_spec_running(spec, commands),
            )
        )
    return found


def drivable_browsers(browsers: list[Browser] | None = None) -> list[Browser]:
    browsers = detect_browsers() if browsers is None else browsers
    return [browser for browser in browsers if browser.drivable]


def undrivable_browsers(browsers: list[Browser] | None = None) -> list[Browser]:
    browsers = detect_browsers() if browsers is None else browsers
    return [browser for browser in browsers if not browser.drivable]


def install_suggestions() -> list[str]:
    """Print-ready commands that install a drivable browser."""
    system = platform.system()
    if system == "Darwin":
        return [
            f"brew install --cask {spec.brew}"
            for spec in _SPECS
            if spec.drivable and spec.brew
        ]
    if system == "Windows":
        return ["winget install -e --id Google.Chrome"]
    return [
        "sudo apt install chromium   # Debian/Ubuntu",
        "sudo dnf install chromium    # Fedora",
    ]


def probe_endpoint(url: str) -> Endpoint:
    """Ask a DevTools endpoint who it is. Only http(s) URLs are probeable."""
    if not url.startswith(("http://", "https://")):
        return Endpoint(url=url, reachable=None, product="websocket endpoint")
    try:
        with urllib.request.urlopen(
            url.rstrip("/") + "/json/version", timeout=_PROBE_TIMEOUT_SECONDS
        ) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError):
        return Endpoint(url=url, reachable=False, product="unreachable")
    product = str(
        payload.get("Browser") or payload.get("product") or "DevTools endpoint"
    )
    return Endpoint(url=url, reachable=True, product=product)


def reachable_endpoints(extra: list[str] | tuple[str, ...] = ()) -> list[Endpoint]:
    """Probe the harness's default ports plus any caller-supplied endpoint."""
    urls = [f"http://127.0.0.1:{port}" for port in DEFAULT_CDP_PORTS]
    urls += [url for url in extra if url and url not in urls]
    return [probe_endpoint(url) for url in urls]


def _spec_path(spec: _Spec, system: str) -> Path | None:
    if system == "Darwin":
        for base in _MAC_APPLICATION_DIRS:
            candidate = base / spec.mac
            if candidate.exists():
                return candidate
        return None
    if system == "Windows":
        if not spec.windows:
            return None
        for root in _WINDOWS_ROOTS:
            candidate = root / spec.windows
            if candidate.exists():
                return candidate
        return None
    for binary in spec.linux:
        found = shutil.which(binary)
        if found:
            return Path(found)
    return None


def _spec_running(spec: _Spec, commands: set[str]) -> bool:
    return bool(spec.process_names() & commands)


def _running_commands() -> set[str]:
    """Executable names currently running. Empty when the OS has no cheap probe."""
    argv = (
        ["tasklist", "/FO", "CSV", "/NH"]
        if platform.system() == "Windows"
        else ["ps", "-Ao", "args="]
    )
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=3.0, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if completed.returncode != 0:
        return set()
    names: set[str] = set()
    for line in completed.stdout.splitlines():
        line = line.strip().strip('"')
        if not line:
            continue
        first = (
            line.split(",")[0]
            if platform.system() == "Windows"
            else line.split(maxsplit=1)[0]
        )
        names.add(Path(first.replace("\\", "/")).name.casefold())
    return names
