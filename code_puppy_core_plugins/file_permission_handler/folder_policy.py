"""Folder approval policy for file edits and deletes.

TODO(roadmap:sandboxing): shell and network sandboxing are deferred; shell commands are not folder-enforced here.
"""

from __future__ import annotations

import enum
import fnmatch
import json
import os
import sys
import tempfile
from pathlib import Path, PurePath

from code_puppy import config

_CONFIG_KEY = "approved_folders"
# Ordered: protected_paths() displays them in this order.
_PROTECTED_DIR_NAMES = (
    ".git",
    ".agents",
    ".codex",
    ".aws",
    ".code_puppy",
    ".ssh",
    ".gnupg",
)
_PROTECTED_FILE_PATTERNS = (
    ".env",
    ".env.*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "id_rsa*",
    "id_ed25519*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.crt",
    "*.cer",
    "*.keystore",
    "*.jks",
)
_TEMP_ENV_VARS = ("TMPDIR", "TEMP", "TMP")
_SYSTEM_TMP = "/tmp"


class Decision(enum.Enum):
    ALLOW = "allow"
    ASK = "ask"


class PolicyError(ValueError):
    """A folder cannot be added to or removed from the user's approved list."""


def _fold(text: str) -> str:
    # macOS and Windows file systems are case-insensitive by default; Linux is not.
    if sys.platform in ("darwin", "win32"):
        return os.path.normcase(text).casefold()
    return text


def _working_directory(cwd: str | None) -> str:
    if cwd is not None:
        return cwd
    try:
        from code_puppy.tools.common import get_working_directory
    except ImportError:
        return os.getcwd()
    return get_working_directory()


def _absolute(path: str, cwd: str | None) -> str:
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return expanded
    return os.path.join(_working_directory(cwd), expanded)


def resolve(path: str, cwd: str | None = None) -> str:
    return os.path.realpath(_absolute(path, cwd))


def _has_protected_part(absolute: str) -> bool:
    protected_dirs = {_fold(name) for name in _PROTECTED_DIR_NAMES}
    if any(_fold(part) in protected_dirs for part in PurePath(absolute).parts):
        return True
    name = _fold(PurePath(absolute).name)
    return any(
        fnmatch.fnmatchcase(name, _fold(pattern))
        for pattern in _PROTECTED_FILE_PATTERNS
    )


def is_protected(path: str) -> bool:
    # Checks the link's own name as well as its target, so `creds.pem -> data.txt` still asks.
    absolute = _absolute(path, None)
    return _has_protected_part(absolute) or _has_protected_part(
        os.path.realpath(absolute)
    )


def _is_within(target: str, root: str) -> bool:
    # Component-wise, so `/tmp-evil` is never inside `/tmp`.
    target_parts = PurePath(_fold(target)).parts
    root_parts = PurePath(_fold(root)).parts
    return target_parts[: len(root_parts)] == root_parts


def _temp_roots() -> list[str]:
    # On Windows `/tmp` resolves to `C:\tmp`, which is not a temp folder.
    system_roots = [_SYSTEM_TMP] if sys.platform != "win32" else []
    candidates = [*system_roots, tempfile.gettempdir()]
    candidates += [os.environ[var] for var in _TEMP_ENV_VARS if os.environ.get(var)]
    return list(dict.fromkeys(os.path.realpath(c) for c in candidates))


def _parse_folder_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list) or not all(
        isinstance(item, str) and os.path.isabs(item) for item in data
    ):
        return []
    return list(dict.fromkeys(data))


def get_user_folders() -> list[str]:
    return _parse_folder_list(config.get_value(_CONFIG_KEY))


def _save_folders(folders: list[str]) -> None:
    config.set_config_value(_CONFIG_KEY, json.dumps(folders))


def always_approved_folders(cwd: str | None = None) -> dict:
    return {
        "project": os.path.realpath(_working_directory(cwd)),
        "temp": _temp_roots(),
    }


def _approved_roots(cwd: str | None) -> list[str]:
    always = always_approved_folders(cwd)
    user = [os.path.realpath(folder) for folder in get_user_folders()]
    return [always["project"], *always["temp"], *user]


def is_approved(path: str, cwd: str | None = None) -> bool:
    target = resolve(path, cwd)
    return any(_is_within(target, root) for root in _approved_roots(cwd))


def decide(file_path: str, cwd: str | None = None) -> Decision:
    absolute = _absolute(file_path, cwd)
    if is_protected(absolute):
        return Decision.ASK
    if is_approved(absolute, cwd):
        return Decision.ALLOW
    return Decision.ASK


def add_user_folder(path: str) -> list[str]:
    if not os.path.isabs(path):
        raise PolicyError(f"Folder path must be absolute: {path}")
    target = os.path.realpath(path)
    if not os.path.exists(target):
        raise PolicyError(f"Folder does not exist: {path}")
    if not os.path.isdir(target):
        raise PolicyError(f"Not a folder: {path}")
    if Path(target).parent == Path(target):
        raise PolicyError("A filesystem root cannot be an approved folder")
    if _fold(target) == _fold(os.path.realpath(Path.home())):
        raise PolicyError("The home folder cannot be approved; approve a subfolder")
    folders = get_user_folders()
    if any(_fold(folder) == _fold(target) for folder in folders):
        return folders
    folders.append(target)
    _save_folders(folders)
    return folders


def remove_user_folder(path: str) -> list[str]:
    target = _fold(resolve(path))
    folders = get_user_folders()
    remaining = [folder for folder in folders if _fold(folder) != target]
    if len(remaining) == len(folders):
        raise PolicyError(f"Folder is not in the approved list: {path}")
    _save_folders(remaining)
    return remaining


def protected_paths() -> list[str]:
    return [f"{name}/" for name in _PROTECTED_DIR_NAMES] + list(
        _PROTECTED_FILE_PATTERNS
    )
