"""Profile persistence; ordinary INI data, never executable Python presets.

The legacy config is the default profile. Selecting a profile only redirects
this process's config I/O; there is deliberately no global 'active' pointer.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from code_puppy import config
from code_puppy.atomic_io import atomic_write_bytes, path_lock
from code_puppy.config_file import load_config
from code_puppy.i18n import t

_active = "default"
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{i}" for prefix in ("com", "lpt") for i in range(1, 10)
}


def validate_name(name: str) -> str:
    if not _NAME.fullmatch(name) or name in _RESERVED:
        raise ValueError(t("profiles.invalid_name"))
    return name


def profile_path(name: str) -> Path:
    validate_name(name)
    root = Path(config.CONFIG_DIR)
    if name == "default":
        return root / "puppy.cfg"
    directory = root / "profiles" / name
    # Do not follow profile aliases outside the profiles directory.
    if (
        (root / "profiles").is_symlink()
        or directory.is_symlink()
        or (directory / "puppy.cfg").is_symlink()
    ):
        raise ValueError(t("profiles.symlink"))
    return directory / "puppy.cfg"


def active_profile() -> str:
    return _active


def list_profiles() -> list[str]:
    root = Path(config.CONFIG_DIR) / "profiles"
    if not root.exists():
        return ["default"]
    return ["default"] + sorted(
        p.name
        for p in root.iterdir()
        if p.name != "default"
        and _NAME.fullmatch(p.name)
        and not p.is_symlink()
        and not (p / "puppy.cfg").is_symlink()
        and (p / "puppy.cfg").is_file()
    )


def create_profile(name: str) -> None:
    """Snapshot current settings without overwriting an existing profile."""
    target = profile_path(name)
    if name == "default":
        raise ValueError(t("profiles.exists", name=name))
    if target.exists():
        raise ValueError(t("profiles.exists", name=name))
    migrate_credentials()
    snapshot = load_config(str(config.CONFIG_FILE))
    from code_puppy.shared_credentials import is_credential_key

    # A concurrently running older Puppy may have written a plaintext key
    # after migration. Never duplicate it into the new profile.
    for section in [snapshot.default_section, *snapshot.sections()]:
        for key in list(snapshot[section]):
            if is_credential_key(key):
                snapshot[section].pop(key, None)
    buffer = io.StringIO()
    snapshot.write(buffer)
    target.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.mkdir(mode=0o700, exist_ok=True)
    with path_lock(str(target)):
        if target.exists():
            raise ValueError(t("profiles.exists", name=name))
        atomic_write_bytes(str(target), buffer.getvalue().encode("utf-8"))


def activate_profile(name: str) -> None:
    """Select a config for this process; missing named profiles fail closed."""
    global _active
    target = profile_path(name)
    if name != "default" and not target.is_file():
        raise ValueError(t("profiles.missing", name=name))
    # Validate accessibility before changing any runtime state.
    load_config(str(target))
    migrate_credentials()
    config.CONFIG_FILE = str(target)
    config.reset_session_model()
    config.clear_model_cache()
    _active = name


def migrate_credentials() -> None:
    from code_puppy.shared_credentials import migrate

    migrate([str(profile_path(name)) for name in list_profiles()])
