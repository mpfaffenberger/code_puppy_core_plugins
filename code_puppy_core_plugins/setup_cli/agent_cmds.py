"""``setup agent`` — manage user-tier JSON agent definitions."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .output import Out, SetupError, confirm


def _agents_dir() -> Path:
    from code_puppy.config import AGENTS_DIR

    return Path(AGENTS_DIR)


def _read_agent(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SetupError(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise SetupError(f"{path}: agent definition must be a JSON object")
    return data


def cmd_agent_list(out: Out) -> int:
    base = _agents_dir()
    rows = []
    if base.is_dir():
        for path in sorted(base.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            rows.append(
                {
                    "file": path.name,
                    "name": data.get("name", path.stem),
                    "description": data.get("description", ""),
                }
            )
    out.result(
        {"agents": rows, "agents_dir": str(base)},
        human=[f"{row['name']}  ({row['file']})" for row in rows]
        or ["(no user JSON agents installed)"],
    )
    return 0


def cmd_agent_add(out: Out, source: str, force: bool) -> int:
    src = Path(source).expanduser()
    if not src.is_file():
        raise SetupError(f"agent source '{source}' is not a file")
    # Validate with the real loader so a broken agent never lands on disk.
    from code_puppy.agents.json_agent import JSONAgent

    try:
        agent = JSONAgent(str(src))
    except Exception as exc:
        raise SetupError(f"invalid agent definition: {exc}") from exc
    dest = _agents_dir() / src.name
    if dest.exists() and not force:
        raise SetupError(f"agent file '{src.name}' already exists (use --force)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    out.result(
        {"name": agent.name, "installed_to": str(dest)},
        human=f"installed agent '{agent.name}'",
    )
    return 0


def cmd_agent_new(out: Out, name: str, description: str) -> int:
    dest = _agents_dir() / f"{name}.json"
    if dest.exists():
        raise SetupError(f"agent file '{dest.name}' already exists")
    payload = {
        "name": name,
        "display_name": name.replace("-", " ").title(),
        "description": description or f"Custom agent '{name}' (edit me).",
        "system_prompt": "You are a helpful coding agent. Replace this prompt.",
        "tools": ["list_files", "read_file", "grep", "create_file"],
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    out.result(
        {"name": name, "path": str(dest)},
        human=f"created agent scaffold at {dest} (edit its system_prompt/tools)",
    )
    return 0


def cmd_agent_remove(out: Out, name: str, assume_yes: bool) -> int:
    base = _agents_dir()
    dest = base / (name if name.endswith(".json") else f"{name}.json")
    if not dest.is_file():
        # Fall back to matching the declared agent name.
        for path in sorted(base.glob("*.json")) if base.is_dir() else []:
            if _read_agent(path).get("name") == name:
                dest = path
                break
    if not dest.is_file():
        raise SetupError(f"no user agent '{name}'")
    if not confirm(f"Remove agent file '{dest.name}'?", assume_yes=assume_yes):
        raise SetupError("aborted", exit_code=2)
    dest.unlink()
    out.result({"file": dest.name, "removed": True}, human=f"removed {dest.name}")
    return 0
