"""``setup plugin`` — non-interactive sibling of the ``/plugins`` menu.

Installation copies *user-tier* plugins only. Installing a plugin is
installing code that will run at startup: the entry file is shown and
confirmation is required unless ``--yes`` is passed.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .output import Out, SetupError, confirm


def _user_plugins_dir() -> Path:
    from code_puppy.plugins import USER_PLUGINS_DIR

    return Path(USER_PLUGINS_DIR)


def cmd_plugin_list(out: Out) -> int:
    from code_puppy.plugins import get_loaded_plugins, get_project_plugin_status
    from code_puppy.plugins.config import get_disabled_plugins

    loaded = get_loaded_plugins()
    disabled = sorted(get_disabled_plugins())
    rows = []
    for tier in ("builtin", "user", "project"):
        for name in loaded.get(tier, []):
            rows.append({"name": name, "tier": tier, "disabled": name in disabled})
    data = {
        "plugins": rows,
        "disabled": disabled,
        "project_status": get_project_plugin_status(),
    }
    human = [
        f"{row['name']}  [{row['tier']}]" + ("  (disabled)" if row["disabled"] else "")
        for row in rows
    ] or ["(no plugins loaded)"]
    out.result(data, human=human)
    return 0


def _looks_like_git_url(source: str) -> bool:
    return source.endswith(".git") or "://" in source or source.startswith("git@")


def _fetch_source(source: str, tmp: Path) -> Path:
    if _looks_like_git_url(source):
        result = subprocess.run(
            ["git", "clone", "--depth", "1", source, str(tmp / "src")],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SetupError(f"git clone failed: {result.stderr.strip()[:400]}")
        return tmp / "src"
    path = Path(source).expanduser()
    if not path.is_dir():
        raise SetupError(f"plugin source '{source}' is not a directory or git URL")
    return path


def cmd_plugin_add(
    out: Out, source: str, name: Optional[str], assume_yes: bool, force: bool
) -> int:
    with tempfile.TemporaryDirectory(prefix="setup-plugin-") as tmp:
        src = _fetch_source(source, Path(tmp))
        entry = src / "register_callbacks.py"
        if not entry.is_file():
            raise SetupError(
                "not a Code Puppy plugin: no register_callbacks.py at its root"
            )
        plugin_name = (name or src.name).strip()
        if not plugin_name or "/" in plugin_name or "\\" in plugin_name:
            raise SetupError(f"invalid plugin name '{plugin_name}'")
        entry_text = entry.read_text(encoding="utf-8", errors="replace")
        if not out.json_mode:
            preview = "\n".join(entry_text.splitlines()[:40])
            print(f"--- {plugin_name}/register_callbacks.py (first 40 lines) ---")
            print(preview)
            print("--- end preview ---")
        if any(
            line.startswith(("from .", "import .")) for line in entry_text.splitlines()
        ):
            out.warn(
                "this plugin uses relative imports, which the user-plugin "
                "loader does not support; it may fail to load as a user plugin"
            )
        dest = _user_plugins_dir() / plugin_name
        if dest.exists() and not force:
            raise SetupError(
                f"user plugin '{plugin_name}' already exists (use --force to replace)"
            )
        if not confirm(
            f"Install '{plugin_name}' as a user plugin? It will run at startup.",
            assume_yes=assume_yes,
        ):
            raise SetupError("aborted", exit_code=2)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(
            src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc")
        )
    out.result(
        {"name": plugin_name, "installed_to": str(dest), "tier": "user"},
        human=f"installed user plugin '{plugin_name}' (loads on next start)",
    )
    return 0


def cmd_plugin_remove(out: Out, name: str, assume_yes: bool) -> int:
    dest = _user_plugins_dir() / name
    if not dest.is_dir():
        raise SetupError(
            f"no user plugin '{name}' (builtin/project plugins are not removable here)"
        )
    if not confirm(f"Remove user plugin '{name}'?", assume_yes=assume_yes):
        raise SetupError("aborted", exit_code=2)
    shutil.rmtree(dest)
    out.result({"name": name, "removed": True}, human=f"removed user plugin '{name}'")
    return 0


def cmd_plugin_set_disabled(out: Out, name: str, disabled: bool) -> int:
    from code_puppy.plugins.config import set_plugin_disabled

    set_plugin_disabled(name, disabled)
    state = "disabled" if disabled else "enabled"
    out.result(
        {"name": name, "disabled": disabled},
        human=f"plugin '{name}' {state} (applies on next start)",
    )
    return 0
