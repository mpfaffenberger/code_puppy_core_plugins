"""Pydantic-AI tools that drive a real browser through browser-harness."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from pydantic_ai import BinaryContent, RunContext, ToolReturn

from . import cli
from . import policy
from .policy import BrowserHarnessError

try:  # Same distribution as the computer-use plugin; reuse its terminal renderer.
    from code_puppy_core_plugins.computer_use.inline_image import emit_inline_image
except ImportError:  # pragma: no cover - the bundle always ships both

    def emit_inline_image(path: str | Path) -> bool:
        del path
        return False


MIN_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 900.0
#: Keeps captures inside the long-edge limit image-aware models resample to.
DEFAULT_SCREENSHOT_MAX_DIM = 1568
_SCREENSHOT_SCRIPT = "print(capture_screenshot(full={full}, max_dim={max_dim}))"


def _clamp_timeout(timeout: float) -> float:
    return min(max(float(timeout), MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)


async def _attempt(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking harness call, turning errors into model-readable results."""
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except BrowserHarnessError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # pragma: no cover - never leak a traceback
        return {"success": False, "error": f"browser-harness failed: {exc}"}


def _consent_blocked() -> dict[str, Any] | None:
    try:
        policy.settings_store.require_enabled()
    except BrowserHarnessError as exc:
        return {"success": False, "error": str(exc)}
    return None


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def register_browser_harness(agent):
    @agent.tool
    async def browser_harness(
        context: RunContext,
        script: str,
        browser_name: str | None = None,
        timeout: float = cli.DEFAULT_TIMEOUT_SECONDS,
    ) -> Any:
        """Run Python against the browser using browser-harness helpers.

        Helpers arrive pre-imported: new_tab, goto_url, page_info, js, cdp,
        click_at_xy, type_text, fill_input, press_key, scroll, capture_screenshot,
        list_tabs, current_tab, switch_tab, close_tab, wait_for_load,
        wait_for_element, wait_for_network_idle, ensure_real_tab, upload_file.
        Print whatever you want back - stdout is the only channel.

        The first navigation of a task is new_tab(url); the attached tab is
        remembered between calls, so do not open another one per script. Prefer
        the accessibility tree (cdp("Accessibility.getFullAXTree")) and js()
        over screenshots to locate elements, then click by coordinate. Stop and
        ask before entering passwords, MFA codes, or completing purchases.

        browser_name selects a named harness daemon (a cloud browser started with
        start_remote_daemon); leave it unset for your local browser.
        """
        del context
        blocked = _consent_blocked()
        if blocked is not None:
            return blocked
        result = await _attempt(
            cli.run_script, script, browser_name, _clamp_timeout(timeout)
        )
        if not isinstance(result, cli.HarnessResult):
            return result
        if result.ok:
            return {"success": True, "output": result.stdout.strip() or "(no output)"}
        return {"success": False, "error": result.failure(), "output": result.stdout}

    return browser_harness


def register_browser_screenshot(agent):
    @agent.tool
    async def browser_screenshot(
        context: RunContext,
        full: bool = False,
        max_dim: int | None = DEFAULT_SCREENSHOT_MAX_DIM,
    ) -> Any:
        """Capture the attached tab and show it inline to the user.

        Pixels alone cannot tell you what is clickable: pair a capture with
        page_info() or js() before acting. Set full=True for the whole scroll
        height, and max_dim=None to keep native resolution.
        """
        del context
        blocked = _consent_blocked()
        if blocked is not None:
            return blocked
        script = _SCREENSHOT_SCRIPT.format(full=bool(full), max_dim=max_dim)
        result = await _attempt(cli.run_script, script)
        if not isinstance(result, cli.HarnessResult):
            return result
        if not result.ok:
            return {"success": False, "error": result.failure()}
        path = _last_line(result.stdout)
        image = Path(path)
        if not image.is_file():
            return {
                "success": False,
                "error": f"browser-harness reported no screenshot file at {path!r}",
                "output": result.stdout,
            }
        metadata = {"success": True, "path": path, "displayed_inline": False}
        content = [
            f"Here is the browser screenshot ({'full page' if full else 'viewport'}):"
        ]
        try:
            metadata["displayed_inline"] = emit_inline_image(path)
            content.append(
                BinaryContent(data=image.read_bytes(), media_type="image/png")
            )
        except OSError as exc:
            return {"success": False, "error": f"Could not read screenshot: {exc}"}
        return ToolReturn(return_value=metadata, content=content, metadata=metadata)

    return browser_screenshot


def register_browser_doctor(agent):
    @agent.tool
    async def browser_doctor(context: RunContext) -> Any:
        """Report browser-harness install, daemon, and browser connection health.

        Use it before blaming a script for a connection problem: the report names
        the exact fix (remote-debugging toggle, mac-approve approval, or starting
        a browser).
        """
        del context
        blocked = _consent_blocked()
        if blocked is not None:
            return blocked
        result = await _attempt(cli.run_command, ["--doctor"], timeout=60.0)
        if not isinstance(result, cli.HarnessResult):
            return result
        report = result.stdout.strip()
        if result.ok:
            return {"success": True, "healthy": True, "report": report or "(no output)"}
        payload: dict[str, Any] = {
            "success": True,
            "healthy": False,
            "report": report or result.failure(),
        }
        fixup = cli.fixup_for(f"{result.stderr}\n{result.stdout}")
        if fixup:
            payload["fix"] = fixup
        return payload

    return browser_doctor


REGISTRARS = {
    "browser_harness": register_browser_harness,
    "browser_screenshot": register_browser_screenshot,
    "browser_doctor": register_browser_doctor,
}
