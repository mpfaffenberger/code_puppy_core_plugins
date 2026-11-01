"""``/approved_folders`` command: list, add or remove folders where edits never prompt.

Paths are passed as plain strings: ``emit_*`` escapes Rich markup in the renderer.
"""

import os
import shlex

from code_puppy.callbacks import register_callback
from code_puppy.messaging import emit_error, emit_info, emit_warning

from code_puppy_core_plugins.file_permission_handler import folder_policy
from code_puppy_core_plugins.file_permission_handler.folder_policy import PolicyError

_COMMAND = "approved_folders"
_USAGE = "Usage: /approved_folders [list | add <path> | remove <path> | protected]"
_POSIX = os.name != "nt"


def _custom_help():
    return [
        (
            _COMMAND,
            "List, add or remove folders where file edits never prompt (protected paths always ask)",
        ),
    ]


def _protected_line() -> str:
    paths = ", ".join(folder_policy.protected_paths())
    return f"Protected (always ask, even inside approved folders): {paths}"


def _show_list() -> None:
    approved = folder_policy.always_approved_folders()
    user_folders = folder_policy.get_user_folders() or [
        "(none saved; add one with /approved_folders add <path>)"
    ]
    lines = [
        "Approved folders (edits here never prompt):",
        f"  Project folder: {approved.get('project') or 'none'}",
        "  Temp folders:",
        *(f"    {path}" for path in approved.get("temp", [])),
        "  Your folders:",
        *(f"    {path}" for path in user_folders),
        _protected_line(),
    ]
    emit_info("\n".join(lines))


def _show_protected() -> None:
    emit_info(_protected_line())


def _add(raw_path: str) -> None:
    expanded = os.path.expanduser(raw_path)
    folder_policy.add_user_folder(expanded)
    emit_info(f"Approved folder saved: {folder_policy.resolve(expanded)}")


def _remove(raw_path: str) -> None:
    expanded = os.path.expanduser(raw_path)
    folder_policy.remove_user_folder(expanded)
    emit_info(f"Approved folder removed: {folder_policy.resolve(expanded)}")


def _split_args(command: str, posix: bool = _POSIX) -> list[str]:
    # POSIX shlex eats backslashes, which mangles Windows paths; non-POSIX shlex keeps
    # the surrounding quotes in each token, so they are stripped here.
    tokens = shlex.split(command, posix=posix)
    if posix:
        return tokens
    return [_strip_matching_quotes(token) for token in tokens]


def _strip_matching_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _handle_custom_command(command: str, name: str):
    if name != _COMMAND:
        return None

    try:
        args = _split_args(command)[1:]
    except ValueError as error:
        emit_error(f"Could not parse /{_COMMAND} arguments: {error}")
        return True

    subcommand, operands = (args[0] if args else "list"), args[1:]
    try:
        if subcommand == "list" and not operands:
            _show_list()
        elif subcommand == "protected" and not operands:
            _show_protected()
        elif subcommand == "add" and len(operands) == 1:
            _add(operands[0])
        elif subcommand == "remove" and len(operands) == 1:
            _remove(operands[0])
        else:
            emit_warning(_USAGE)
    except PolicyError as error:
        emit_error(str(error))
    return True


register_callback("custom_command_help", _custom_help)
register_callback("custom_command", _handle_custom_command)
