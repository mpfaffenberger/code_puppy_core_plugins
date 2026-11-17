"""Locate and drive the ``browser-harness`` CLI as a subprocess.

Everything that talks to the outside world lives here: executable discovery,
environment composition, timeouts, and translating the harness's own failure
strings into fixes that actually work.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import policy
from .policy import BrowserHarnessError

EXECUTABLE_ENV_VAR = "CODE_PUPPY_BROWSER_HARNESS_BIN"
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_CAPTURED_CHARS = 20_000
INSTALL_COMMAND = "uv tool install --python 3.12 --upgrade --force browser-harness"
_DAEMON_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
_UV_TOOL_BIN = Path.home() / ".local" / "bin" / "browser-harness"

# Substrings the harness emits when a connection cannot be made, mapped to the
# fix that clears it. Source of truth: src/browser_harness/daemon.py and
# admin.py in browser-use/browser-harness.
_FIXUPS: tuple[tuple[str, str], ...] = (
    (
        "permission-blocked",
        "Chrome showed its 'Allow remote debugging?' sheet. Run "
        "`browser-harness mac-approve`, then retry.",
    ),
    (
        "remote debugging is turned off",
        "Open chrome://inspect/#remote-debugging and tick 'Allow remote debugging "
        "for this browser instance', then retry.",
    ),
    (
        "devtoolsactiveport not found",
        "No Chrome profile with remote debugging was found. Open "
        "chrome://inspect/#remote-debugging and tick the toggle, or point Code "
        "Puppy at an explicit endpoint with `/browser connect <devtools-url>`.",
    ),
    (
        "chrome-not-running",
        "No Chromium-family browser is running. Start one; Code Puppy's `/browser "
        "status` lists the ones installed here.",
    ),
)


@dataclass(frozen=True)
class HarnessResult:
    """One completed ``browser-harness`` invocation. Both streams are capped."""

    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def failure(self) -> str:
        """A failure description with the concrete next step appended."""
        detail = (self.stderr or self.stdout).strip() or "no output"
        if self.timed_out:
            detail += " (timed out)"
        fixup = fixup_for(f"{self.stderr}\n{self.stdout}")
        return detail if fixup is None else f"{detail}\n\nFix: {fixup}"


def fixup_for(text: str) -> str | None:
    """Return the documented fix for a known harness error, if any matches."""
    lowered = text.casefold()
    for needle, fix in _FIXUPS:
        if needle in lowered:
            return fix
    return None


def executable() -> str | None:
    """Resolve the ``browser-harness`` entry point, honouring an override."""
    override = os.environ.get(EXECUTABLE_ENV_VAR, "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(override)
        if found:
            return found
        raise BrowserHarnessError(
            f"{EXECUTABLE_ENV_VAR} points at {override!r}, which is not an "
            "executable. Unset it or fix the path."
        )
    return shutil.which("browser-harness") or (
        str(_UV_TOOL_BIN) if _UV_TOOL_BIN.is_file() else None
    )


def installed() -> bool:
    return executable() is not None


def require_executable() -> str:
    path = executable()
    if path is None:
        raise BrowserHarnessError(
            "browser-harness is not installed. Install it with:\n  "
            f"{INSTALL_COMMAND}\nThen run `/browser status` to check the "
            "connection."
        )
    return path


def version() -> str | None:
    """Return the installed harness version, or None when unavailable."""
    path = executable()
    if path is None:
        return None
    return (
        _invoke([path, "--version"], timeout=15.0, env=environment()).stdout.strip()
        or None
    )


def environment(daemon: str | None = None) -> dict[str, str]:
    """Compose the child environment without clobbering explicit user config.

    An endpoint already present in the ambient environment always wins: the
    harness treats ``BU_CDP_URL``/``BU_CDP_WS`` as an explicit override, and a
    saved Code Puppy setting must not silently replace it.
    """
    env = dict(os.environ)
    endpoint = policy.settings_store.endpoint()
    if endpoint and ambient_endpoint(env) is None:
        env["BU_CDP_URL" if endpoint.startswith("http") else "BU_CDP_WS"] = endpoint
    if daemon:
        if not _DAEMON_NAME_RE.match(daemon):
            raise BrowserHarnessError(
                f"Invalid browser name {daemon!r}: use 1-64 letters, digits, '-' "
                "or '_'."
            )
        env["BU_NAME"] = daemon
    return env


def ambient_endpoint(env: dict[str, str] | None = None) -> str | None:
    """Return an endpoint the user exported, which outranks the saved setting."""
    env = dict(os.environ) if env is None else env
    return env.get("BU_CDP_WS") or env.get("BU_CDP_URL") or None


def run_script(
    script: str, daemon: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> HarnessResult:
    """Execute a helper script with the harness helpers pre-imported."""
    return _invoke(
        [require_executable()],
        timeout=timeout,
        env=environment(daemon),
        input_text=script,
    )


def run_command(
    args: list[str], timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> HarnessResult:
    """Run a ``browser-harness`` sub-command such as ``--doctor``."""
    return _invoke([require_executable(), *args], timeout=timeout, env=environment())


def _invoke(
    argv: list[str],
    timeout: float,
    env: dict[str, str],
    input_text: str | None = None,
) -> HarnessResult:
    # ``input=`` implies a pipe; sub-commands get DEVNULL so an interactive
    # prompt inside the harness can never hang Code Puppy's UI.
    streams: dict[str, object] = (
        {} if input_text is not None else {"stdin": subprocess.DEVNULL}
    )
    try:
        completed = subprocess.run(  # noqa: S603 - argv is our resolved entry point
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
            **streams,
        )
    except subprocess.TimeoutExpired as exc:
        return HarnessResult(
            ok=False,
            exit_code=-1,
            stdout=_cap(_text(exc.stdout)),
            stderr=_cap(_text(exc.stderr)) or "browser-harness timed out",
            timed_out=True,
        )
    except FileNotFoundError as exc:  # pragma: no cover - resolved path vanished
        raise BrowserHarnessError(f"browser-harness could not be run: {exc}") from exc
    return HarnessResult(
        ok=completed.returncode == 0,
        exit_code=completed.returncode,
        stdout=_cap(completed.stdout),
        stderr=_cap(completed.stderr),
    )


def _cap(value: str | None) -> str:
    text = value or ""
    return text if len(text) <= MAX_CAPTURED_CHARS else text[:MAX_CAPTURED_CHARS] + "…"


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value
