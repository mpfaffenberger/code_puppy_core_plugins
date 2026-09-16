"""Register browser-harness browser control as an opt-in builtin plugin.

The plugin stays silent until the user consents with ``/browser enable``:
driving a real, signed-in browser deserves the same explicit opt-in that macOS
Computer Use requires.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from code_puppy.callbacks import register_callback
from code_puppy.messaging import emit_info

from . import cli
from . import policy

_SKILL_PATH = Path(__file__).with_name("SKILL.md")
_TOOL_NAMES = ("browser_harness", "browser_screenshot", "browser_doctor")


def _installed() -> bool:
    try:
        return cli.installed()
    except Exception:  # pragma: no cover - a bad override must not break startup
        return False


def _register_tools() -> list[dict[str, Any]]:
    if not policy.settings_store.is_enabled():
        return []

    from .tools import REGISTRARS

    return [{"name": name, "register_func": REGISTRARS[name]} for name in _TOOL_NAMES]


def _register_agent_tools(agent_name: str | None = None) -> list[str]:
    del agent_name
    return list(_TOOL_NAMES) if policy.settings_store.is_enabled() else []


def _register_skills() -> list[dict[str, str]]:
    if not policy.settings_store.is_enabled():
        return []
    return [{"name": "browser-harness", "skill_md_path": str(_SKILL_PATH)}]


def _load_prompt() -> str | None:
    if not policy.settings_store.is_enabled():
        return None
    return (
        "Browser control runs inside the user's real, signed-in browser through "
        "browser-harness. Use the browser_harness tool when a task needs those "
        "sessions; use plain HTTP for public pages and the web-retriever agent "
        "for sandboxed scraping. The first navigation of a task is new_tab(url): "
        "the harness keeps one tab attached, so do not reopen tabs per call or "
        "close tabs you did not create. Locate elements in the accessibility "
        "tree and verify each action with page_info() or js() rather than "
        "screenshots. Never submit passwords, MFA codes, payments, deletions, or "
        "published content without asking first - being signed in is not consent. "
        "On a connection error, call browser_doctor() and apply the fix it "
        "prints instead of retrying blindly. Firefox and Safari cannot be driven "
        "at all (they expose no CDP endpoint); Chrome, Chromium, Brave, Edge, "
        "Arc, and Helium can be."
    )


def _startup() -> None:
    if _installed() and policy.settings_store.consent_state() == "unset":
        emit_info(
            "browser-harness is installed but browser control is off. Run "
            "`/browser status` to see connection health, or `/browser enable` to "
            "let Code Puppy drive this machine's Chromium browser."
        )


def _custom_help():
    from .commands import command_help

    return command_help()


def _custom_command(command: str, name: str):
    from .commands import handle_command

    return handle_command(command, name)


register_callback("startup", _startup)
register_callback("register_tools", _register_tools)
register_callback("register_agent_tools", _register_agent_tools)
register_callback("register_skills", _register_skills)
register_callback("load_prompt", _load_prompt)
register_callback("custom_command_help", _custom_help)
register_callback("custom_command", _custom_command)
