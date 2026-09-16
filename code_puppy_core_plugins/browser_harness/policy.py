"""Persisted consent and connection target for the browser-harness plugin."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from code_puppy.config import CONFIG_DIR

POLICY_PATH = Path(CONFIG_DIR) / "browser_harness_policy.json"
ENDPOINT_SCHEMES = ("http://", "https://", "ws://", "wss://")


class BrowserHarnessError(RuntimeError):
    """Raised when the harness is unusable or refused a request."""


class SettingsStore:
    """Remember the one-time opt-in and an optional explicit CDP endpoint.

    The endpoint mirrors browser-harness's own escape hatch: an HTTP DevTools
    URL (``BU_CDP_URL``) or a WebSocket URL (``BU_CDP_WS``). Leaving it unset
    defers browser discovery entirely to the harness, which auto-detects the
    running Chromium-family browser.
    """

    def __init__(self, path: Path = POLICY_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"enabled": None, "endpoint": None}
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"enabled": None, "endpoint": None}
        enabled = payload.get("enabled")
        endpoint = payload.get("endpoint")
        return {
            "enabled": enabled if isinstance(enabled, bool) else None,
            "endpoint": endpoint if isinstance(endpoint, str) and endpoint else None,
        }

    def _save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True))
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    # --- consent ---------------------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            payload = self._load()
            payload["enabled"] = enabled
            self._save(payload)

    def is_enabled(self) -> bool:
        """Return whether the user explicitly opted in to browser control."""
        with self._lock:
            return self._load()["enabled"] is True

    def consent_state(self) -> str:
        with self._lock:
            enabled = self._load()["enabled"]
        return "unset" if enabled is None else "enabled" if enabled else "disabled"

    def require_enabled(self) -> None:
        with self._lock:
            state = self.consent_state()
        if state == "unset":
            raise BrowserHarnessError(
                "Browser Harness needs your one-time permission before its first "
                "use. It drives your real, signed-in browser: it can read pages, "
                "click, type, and download. Run `/browser enable` to allow it, or "
                "`/browser disable` to keep it off. You can change this later with "
                "the same commands."
            )
        if state == "disabled":
            raise BrowserHarnessError(
                "Browser Harness is disabled in settings. Run `/browser enable` to "
                "turn it on."
            )

    # --- connection target ----------------------------------------------
    def endpoint(self) -> str | None:
        with self._lock:
            return self._load()["endpoint"]

    def set_endpoint(self, endpoint: str) -> None:
        normalized = endpoint.strip().rstrip("/")
        if not normalized.lower().startswith(ENDPOINT_SCHEMES):
            raise BrowserHarnessError(
                f"Invalid CDP endpoint {endpoint!r}: expected an http(s):// DevTools "
                "URL (for example http://127.0.0.1:9222) or a ws(s):// URL."
            )
        with self._lock:
            payload = self._load()
            payload["endpoint"] = normalized
            self._save(payload)

    def clear_endpoint(self) -> None:
        with self._lock:
            payload = self._load()
            payload["endpoint"] = None
            self._save(payload)

    def status(self) -> dict[str, Any]:
        with self._lock:
            payload = self._load()
        return {
            "consent": payload["enabled"],
            "endpoint": payload["endpoint"],
            "endpoint_source": "saved" if payload["endpoint"] else "auto-discovery",
        }


settings_store = SettingsStore()
