"""Leave a breadcrumb on exit: ``Resume this session with: uvx code-puppy --resume <name>``.

Hooks ``shutdown``, which ``cli_runner.main`` fires *after* the persistent UI
and message renderers are torn down. That makes it the last thing on screen,
but it also means the message bus is gone, so we write straight to stdout.

The hint only appears when autosave is on and the session file exists on
disk, so we never advertise a name with nothing saved behind it (nothing
said yet, or a fresh ``/clear``) or a stale snapshot (autosave disabled).
"""

from __future__ import annotations

import logging
from importlib.resources import files
from pathlib import Path

from code_puppy.callbacks import register_callback
from code_puppy.config import (
    AUTOSAVE_DIR,
    get_auto_save_session,
    get_current_session_name,
    get_headless_mode,
)
from code_puppy.i18n import register_plugin_catalog, t
from code_puppy.session_storage import build_session_paths

register_plugin_catalog(files(__package__).joinpath("locales"))

logger = logging.getLogger(__name__)

# The README's launch method; works whether or not code-puppy is installed.
_LAUNCH_COMMAND = "uvx code-puppy"


def _saved_session_name() -> str | None:
    """Return the current session name if it has been persisted, else None."""
    name = get_current_session_name()
    paths = build_session_paths(Path(AUTOSAVE_DIR), name)
    if paths.json_path.is_file() or paths.pickle_path.is_file():
        return name
    return None


def _resume_arg(session_name: str) -> str:
    # argparse reads a leading "-" as an option; the "=" form keeps it a value.
    separator = "=" if session_name.startswith("-") else " "
    return f"--resume{separator}{session_name}"


def _format_resume_hint(session_name: str) -> str:
    return (
        f"\n{t('plugin.resume-hint.header')}\n"
        f"  {_LAUNCH_COMMAND} {_resume_arg(session_name)}\n"
    )


def _on_shutdown() -> None:
    try:
        # Headless ``-p`` stdout is often machine-parsed; with autosave off,
        # the file on disk is a stale snapshot rather than "this session".
        if get_headless_mode() or not get_auto_save_session():
            return
        session_name = _saved_session_name()
        if session_name:
            print(_format_resume_hint(session_name), flush=True)
    except Exception:
        # A goodbye nicety must never turn a clean exit into a traceback.
        logger.debug("resume_hint: unable to print resume hint", exc_info=True)


register_callback("shutdown", _on_shutdown)
