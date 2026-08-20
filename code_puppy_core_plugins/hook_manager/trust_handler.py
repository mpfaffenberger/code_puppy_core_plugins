"""`/hooks trust` — accept, inspect, or revoke project-level hook trust.

Project ``.claude/settings.json`` files can define hooks that execute
arbitrary shell commands at Code Puppy lifecycle events, so they are
ignored until the user explicitly trusts them. This handler drives that
trust ceremony:

* ``/hooks trust``          → preview the project hooks + trust status.
* ``/hooks trust accept``   → record trust and reload the running engine.
* ``/hooks trust revoke``   → drop trust for this project and reload.
* ``/hooks trust status``   → alias for the bare preview.

Trust is subtree-content-hashed and stored user-side; see
:mod:`code_puppy_core_plugins.claude_code_hooks.trust`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from code_puppy.messaging import emit_error, emit_info, emit_success, emit_warning

# SECURITY: use the executor-side, CWD-only discovery helper. Do NOT use
# hook_manager.config._find_settings_path — that walks ancestor directories
# and would silently widen the trust ceremony beyond the user's current
# project, re-introducing exactly the "hostile parent dir" attack surface
# this feature exists to eliminate.
from code_puppy_core_plugins.claude_code_hooks import trust as _trust
from code_puppy_core_plugins.claude_code_hooks.register_callbacks import (
    reload_hook_engine,
)

logger = logging.getLogger(__name__)


def handle_trust_subcommand(args: List[str]) -> bool:
    """Dispatch ``/hooks trust [accept|revoke|status]``.

    Args are the tokens AFTER the ``trust`` verb. Returns ``True`` to
    signal the slash-command router that the command was handled (even
    for the "unknown action" case — the user gets a helpful error rather
    than a bare-fallthrough TUI launch).
    """
    action = args[0].lower() if args else "preview"

    try:
        project_root = Path(os.getcwd())
        settings_file = _trust.get_project_hooks_settings_file(project_root)

        if action == "revoke":
            _revoke(project_root, settings_file)
        elif action == "accept":
            _accept(project_root, settings_file)
        elif action in ("status", "preview", ""):
            _preview(project_root, settings_file)
        else:
            emit_error(f"Unknown '/hooks trust' action: {action}")
            emit_info("Usage: /hooks trust [accept|revoke|status]")
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Error handling /hooks trust: %s", exc, exc_info=True)
        emit_error(f"Error handling /hooks trust: {exc}")
    return True


# ---------- actions ----------------------------------------------------------


def _preview(project_root: Path, settings_file: Optional[Path]) -> None:
    """Show the project hooks file, its declared hooks, and trust status."""
    if settings_file is None:
        emit_info(
            "No project hooks file found. Create '.claude/settings.json' in "
            "this repo with a 'hooks' block to define project-scoped hooks."
        )
        return

    subtree = _trust._extract_hooks_subtree(settings_file)
    status = _trust.get_trust_status(project_root, settings_file)

    emit_info(f"Project hooks file: {settings_file}")
    emit_info(f"Trust status:       {_status_label(status)}")
    emit_info("")

    if subtree is None:
        emit_warning(
            "The file is present but not valid JSON, or has no 'hooks' object. "
            "Nothing to trust."
        )
        return
    if not _trust._has_effective_hooks(subtree):
        emit_info("The 'hooks' block is empty (or comment-only). Nothing to trust.")
        return

    emit_info("Declared hooks:")
    for event_type, hook_count, command_summary in _iter_hook_summary(subtree):
        emit_info(f"  \u2022 {event_type}: {hook_count} command(s)")
        for command_line in command_summary:
            emit_info(f"      $ {command_line}")
    emit_info("")

    if status == _trust.TRUSTED:
        emit_success(
            "Trusted. These hooks will run on the next Code Puppy startup "
            "(or when Code Puppy fires the relevant lifecycle event). Run "
            "'/hooks trust revoke' to disable them."
        )
    else:
        verb = (
            "changed since you trusted it"
            if status == _trust.CHANGED
            else "not trusted"
        )
        emit_warning(
            f"This hooks config is {verb}, so its hooks will NOT run. These "
            "hooks can execute arbitrary shell commands. If you trust this "
            "repo, run '/hooks trust accept'."
        )


def _accept(project_root: Path, settings_file: Optional[Path]) -> None:
    if settings_file is None:
        emit_error(
            "No project hooks file found at '.claude/settings.json'. Nothing to trust."
        )
        return
    if _trust.trust_project_hooks(project_root):
        # Rebuild the running engine so future lifecycle events pick up the
        # newly trusted project hooks without needing a process restart.
        reload_hook_engine()
        emit_success(
            f"Trusted {settings_file}. Project hooks will run on the next "
            "lifecycle event (SessionStart still waits until next boot)."
        )
    else:
        emit_error(
            f"Could not trust project hooks at {settings_file} (unreadable "
            "file, missing/empty 'hooks' subtree, malformed JSON, or trust "
            "store write failure)."
        )


def _revoke(project_root: Path, settings_file: Optional[Path]) -> None:
    if _trust.revoke_project_hooks(project_root):
        # Rebuild the running engine so already-registered project hooks
        # stop firing on subsequent lifecycle events.
        reload_hook_engine()
        target = settings_file if settings_file is not None else project_root
        emit_success(
            f"Revoked trust for project hooks at {target}. They will no longer run."
        )
    else:
        emit_info("This project's hooks were not trusted. Nothing to revoke.")


# ---------- helpers ----------------------------------------------------------


def _status_label(status: str) -> str:
    return {
        _trust.TRUSTED: "trusted",
        _trust.CHANGED: "changed (re-accept needed)",
        _trust.UNTRUSTED: "untrusted",
    }.get(status, status)


def _iter_hook_summary(subtree: Dict[str, Any]):
    """Yield ``(event_type, hook_count, [command_lines])`` per event.

    Renders the Claude Code hook schema (each event maps to a list of
    hook groups, each group has a ``hooks`` list of ``{type, command}``
    dicts). Non-conforming entries are surfaced as ``<opaque>`` so a
    user preview never crashes on unexpected shapes.
    """
    for event_type in sorted(k for k in subtree if not k.startswith("_")):
        groups = subtree.get(event_type)
        if not isinstance(groups, list):
            yield event_type, 0, ["<non-list value; skipped at load time>"]
            continue
        commands: List[str] = []
        for group in groups:
            for hook in _iter_group_hooks(group):
                commands.append(_describe_hook(hook))
        yield event_type, len(commands), commands


def _iter_group_hooks(group: Any):
    if not isinstance(group, dict):
        return
    hooks = group.get("hooks")
    if isinstance(hooks, list):
        yield from hooks


def _describe_hook(hook: Any) -> str:
    if not isinstance(hook, dict):
        return f"<non-object hook: {hook!r}>"
    hook_type = hook.get("type", "?")
    if hook_type == "command":
        return str(hook.get("command", "<missing command>"))
    return f"<{hook_type} hook>"
