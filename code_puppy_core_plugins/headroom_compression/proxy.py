"""headroom proxy lifecycle -- start, stop, health-check, URL redirect.

Generic by design: the upstream URL to proxy is whatever the user explicitly
configured via ``/headroom enable <url>`` (config.py), never a hardcoded
hostname. The subprocess inherits the full parent environment unmodified, so
any org-specific CA bundle / corporate proxy already set in the user's shell
carries through automatically -- this plugin does not need to know about it.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_PROXY_PORT = 8787
_PROXY_URL = f"http://127.0.0.1:{_PROXY_PORT}"

_proxy_process: Optional[subprocess.Popen] = None
_proxy_active: bool = False
_upstream_url: str = ""


def _headroom_bin() -> Optional[str]:
    """Return path to the headroom binary if installed, else None."""
    candidate = os.path.join(sys.prefix, "bin", "headroom")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return shutil.which("headroom")


def _is_proxy_healthy(expected_upstream: Optional[str] = None) -> bool:
    """Health-check the proxy on our fixed port, verifying it's actually OURS.

    A bare ``GET /health`` returning < 500 can't tell "our subprocess is
    healthy" apart from "something else entirely is healthy on this port" --
    headroom's own cold start (torch/transformers imports) takes ~13s before
    it even attempts to bind, so during that window a stale orphaned proxy,
    or a totally unrelated standalone ``headroom proxy``/``headroom wrap``
    someone else is running (8787 is also headroom's own default port), can
    answer in our subprocess's place -- reporting false success while our
    real child is still starting up (and will shortly die on the bind
    conflict, unnoticed, since its stdout/stderr are discarded).

    When ``expected_upstream`` is given, this also confirms the proxy's own
    reported ``config.anthropic_api_url`` matches what we asked it to front.
    Loopback callers (this always is one) get that config block in the
    response (see headroom's ``/health`` route); a missing or mismatched
    field means "not proven to be ours" and counts as unhealthy, never as
    healthy-by-default.
    """
    try:
        import httpx

        r = httpx.get(f"{_PROXY_URL}/health", timeout=2.0)
        if r.status_code >= 500:
            return False
        if expected_upstream is None:
            return True
        payload = r.json()
        if payload.get("service") != "headroom-proxy":
            return False
        reported = (payload.get("config") or {}).get("anthropic_api_url") or ""
        return reported.rstrip("/") == expected_upstream.rstrip("/")
    except Exception:
        return False


def start_proxy(upstream_url: str) -> bool:
    """Start the headroom proxy in front of ``upstream_url``.

    Returns True if the proxy is confirmed healthy AND confirmed to be
    fronting ``upstream_url`` specifically, False otherwise -- the caller
    always continues either way (never bricks code-puppy).

    Does NOT short-circuit on "something healthy is already on the port" --
    a stale orphaned process (or an unrelated headroom instance someone else
    is running -- 8787 is headroom's own default port too) could be
    listening there for a *different* upstream, and silently adopting it
    would misroute this session's credentialed requests to the wrong host.
    ``_is_proxy_healthy`` is always called with the expected upstream so a
    foreign occupant of the port is never mistaken for ours, regardless of
    whether it answers before or after our own child finishes starting.
    """
    global _proxy_process, _proxy_active, _upstream_url

    headroom = _headroom_bin()
    if not headroom:
        return False

    if (
        _proxy_process is not None
        and _upstream_url == upstream_url
        and _proxy_process.poll() is None
        and _is_proxy_healthy(upstream_url)
    ):
        _proxy_active = True
        return True

    try:
        _proxy_process = subprocess.Popen(
            [
                headroom,
                "proxy",
                "--port",
                str(_PROXY_PORT),
                "--anthropic-api-url",
                upstream_url,
                "--stateless",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.debug("headroom_compression: failed to start proxy: %s", exc)
        return False

    # Wait up to 30s for the proxy to become healthy. headroom's own
    # imports (torch, transformers, tree-sitter-language-pack for its
    # code-aware compression) are heavy -- measured cold-start on a normal
    # dev machine is ~13s, so a shorter budget silently fails on a fresh
    # install even though the proxy would have come up fine given a moment
    # longer. Bail early if the subprocess exits outright instead of
    # polling a corpse for the full budget.
    for _ in range(60):
        if _proxy_process.poll() is not None:
            break
        if _is_proxy_healthy(upstream_url):
            _proxy_active = True
            _upstream_url = upstream_url
            return True
        time.sleep(0.5)

    _kill_proxy_process()
    return False


def _kill_proxy_process() -> None:
    """Terminate, escalating to kill(), so our own child can never outlive
    us holding the port."""
    global _proxy_process
    if _proxy_process is None:
        return
    try:
        _proxy_process.terminate()
        _proxy_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            _proxy_process.kill()
            _proxy_process.wait(timeout=5)
        except Exception:
            pass
    except Exception:
        pass
    _proxy_process = None


def stop_proxy() -> None:
    global _proxy_active
    _proxy_active = False
    _kill_proxy_process()


def restart_proxy() -> bool:
    from . import config

    upstream = _upstream_url or config.get_upstream_url()
    stop_proxy()
    return start_proxy(upstream) if upstream else False


def is_active() -> bool:
    return _proxy_active


def _rebuild_agent_model() -> None:
    """Best-effort rebuild of the main agent's cached model client.

    code-puppy builds the pydantic-ai model client once and caches it
    (``BaseAgent._code_generation_agent``) until something forces a rebuild
    -- a model switch, but not a proxy enable/disable/restart/fallback.
    Without this, ``/headroom disable`` kills the proxy but every later turn
    (and every retry, which reuses the same cached client) still points at
    ``127.0.0.1:<port>`` and fails to connect for the rest of the session.
    Never raises: on an older code-puppy without this API, or before any
    agent has been built yet, the change simply takes effect on the next
    natural rebuild instead -- same as before this fix, not worse.
    """
    try:
        from code_puppy.agents.agent_manager import get_current_agent

        get_current_agent().reload_code_generation_agent()
    except Exception:
        pass


def check_and_fallback() -> bool:
    """If the proxy was active but is now unhealthy, deactivate it.

    Returns True if fallback was applied (caller should warn the user).
    """
    global _proxy_active
    if not _proxy_active:
        return False
    if not _is_proxy_healthy(_upstream_url):
        _proxy_active = False
        return True
    return False


_CONNECTION_FAILURE_MARKERS = (
    "connection",
    "connect",
    "timeout",
    "network",
    "readerror",
    "remoteprotocolerror",
    "proxyerror",
)


def _looks_like_connection_failure(exception, _depth: int = 0) -> bool:
    """Check the exception's own type AND its __cause__/__context__ chain --
    pydantic-ai commonly wraps the real transport error (e.g.
    UnexpectedModelBehavior wrapping an httpx.ConnectError)."""
    if _depth > 5 or exception is None:
        return False
    exc_type = type(exception)
    haystack = f"{exc_type.__module__}.{exc_type.__name__}".lower()
    if isinstance(exception, (ConnectionError, TimeoutError, OSError)):
        return True
    if any(m in haystack for m in _CONNECTION_FAILURE_MARKERS):
        return True
    return _looks_like_connection_failure(
        exception.__cause__ or exception.__context__, _depth + 1
    )


def on_agent_exception_check_proxy(
    exception: BaseException, *_: object, **__: object
) -> None:
    """Registered on the generic ``agent_exception`` hook (already in core).

    Detects proxy failure on connection-flavored exceptions and deactivates
    the redirect (and stops the proxy) so the next call goes direct to the
    real upstream.
    """
    if not _proxy_active:
        return None
    if not _looks_like_connection_failure(exception):
        return None
    if check_and_fallback():
        stop_proxy()
        _rebuild_agent_model()
        try:
            from code_puppy.messaging import emit_warning

            emit_warning(
                "headroom proxy is unreachable -- switched to direct upstream.\n"
                "  Run /headroom restart to re-enable compression."
            )
        except Exception:
            pass
    return None


def _origin_and_path(url: str):
    """Return ((scheme, host, port), path) with scheme-default ports filled
    in, so ``https://x`` and ``https://x:443`` compare equal."""
    parts = urlsplit(url)
    default_port = {"http": 80, "https": 443}.get(parts.scheme)
    port = parts.port or default_port
    return (parts.scheme, parts.hostname, port), parts.path


def resolve_custom_endpoint_url(url: str) -> Optional[str]:
    """Registered on the core ``resolve_custom_endpoint_url`` phase.

    Only rewrites URLs matching the explicitly configured upstream's scheme,
    host, port, AND path prefix (see config.enable()) -- host-only matching
    would false-positive on a shared multi-API gateway (e.g. the same host
    serving both /anthropic and /openai behind different paths) and ship
    that other API's credentials into this proxy. Any endpoint that isn't
    an exact origin + path-prefix match is left untouched. Returns None
    whenever the proxy isn't active or the URL isn't ours.

    The rewritten path is the request's path with the upstream's own prefix
    STRIPPED, not the original full path. headroom's dedicated
    ``/v1/messages`` route reconstructs the full upstream URL itself from
    ``--anthropic-api-url`` (the exact ``upstream_url`` this plugin passed
    at proxy startup) plus whatever path the proxy receives -- so forwarding
    the untouched original path double-prepends the prefix for any upstream
    whose path isn't empty (``https://gw/llm/claude`` + incoming
    ``/llm/claude/v1/messages`` would hit the proxy at
    ``/llm/claude/v1/messages`` and get forwarded to
    ``https://gw/llm/claude/llm/claude/v1/messages``). Stripping the prefix
    first means the proxy always receives the bare ``/v1/messages``-shaped
    suffix and headroom reconstructs the correct, undoubled upstream URL
    for every configured prefix, not just the empty-prefix case.
    """
    if not _proxy_active or not _upstream_url:
        return None

    upstream_origin, upstream_path = _origin_and_path(_upstream_url)
    if upstream_origin[1] is None:
        return None  # malformed upstream URL (e.g. no scheme) -- never match

    url_origin, url_path = _origin_and_path(url)
    if url_origin != upstream_origin:
        return None
    trimmed = upstream_path.rstrip("/")
    if not (url_path == upstream_path or url_path.startswith(trimmed + "/")):
        return None

    suffix = url_path[len(trimmed):] or "/"
    if not suffix.startswith("/"):
        suffix = "/" + suffix

    parts = urlsplit(url)
    proxy_parts = urlsplit(_PROXY_URL)
    return urlunsplit(
        (
            proxy_parts.scheme,
            proxy_parts.netloc,
            suffix,
            parts.query,
            parts.fragment,
        )
    )
