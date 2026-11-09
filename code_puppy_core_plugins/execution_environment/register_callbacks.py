"""Add OS/shell facts to runtime instructions, without changing core agents."""

import logging
import os
import platform
import sys

from code_puppy.callbacks import register_callback

logger = logging.getLogger(__name__)


def get_execution_environment_prompt() -> str:
    """Describe core command_runner's subprocess(shell=True) defaults.

    No executable override is supplied by the local runner. SHELL and the
    interactive terminal are not authoritative: Windows Python uses COMSPEC,
    POSIX Python uses /bin/sh, and Android Python uses /system/bin/sh.
    Revisit this provider if the runner adds a configurable execution backend.
    """
    system = platform.system() or sys.platform
    if sys.platform == "win32":
        shell = os.environ.get("COMSPEC") or os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"
        )
        guidance = (
            "Use syntax compatible with this Windows command shell. "
            "For cmd.exe, use %NAME% for environment variables and Windows "
            "commands such as dir and where; do not use PowerShell cmdlets "
            "or POSIX shell syntax unless explicitly invoking that interpreter."
        )
    else:
        shell = "/system/bin/sh" if hasattr(sys, "getandroidapilevel") else "/bin/sh"
        guidance = (
            "Use POSIX sh-compatible syntax, not Bash-specific syntax unless "
            "explicitly invoking Bash. Do not assume GNU-only utilities or flags "
            "are available on every POSIX operating system."
        )
    return (
        "\n\nExecution environment (local shell tool):\n"
        f"- Operating system: {system}\n"
        f"- Command shell: {shell} (subprocess shell=True)\n"
        f"- {guidance}\n"
        "- The interactive terminal may use a different shell. These facts apply "
        "to local commands, not remote hosts, containers, or explicitly invoked shells."
    )


def _on_load_prompt() -> str | None:
    """Compute current host facts at prompt assembly; never block agent startup."""
    try:
        return get_execution_environment_prompt()
    except Exception:
        logger.debug(
            "Unable to describe the local execution environment", exc_info=True
        )
        return None


register_callback("load_prompt", _on_load_prompt)
