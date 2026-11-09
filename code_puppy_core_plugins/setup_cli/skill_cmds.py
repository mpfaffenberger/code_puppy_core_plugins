"""``setup skill`` — manage user-tier skill directories on disk."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .output import Out, SetupError, confirm
from .plugin_cmds import _looks_like_git_url


def _skills_dir() -> Path:
    from code_puppy.config import SKILLS_DIR

    return Path(SKILLS_DIR)


def _frontmatter_name(skill_md: Path) -> str:
    try:
        lines = skill_md.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    in_block = False
    for line in lines[:40]:
        if line.strip() == "---":
            if in_block:
                break
            in_block = True
            continue
        if in_block and line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip('"')
    return ""


def cmd_skill_list(out: Out) -> int:
    base = _skills_dir()
    rows = []
    if base.is_dir():
        for child in sorted(base.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.is_file():
                rows.append(
                    {
                        "directory": child.name,
                        "name": _frontmatter_name(skill_md) or child.name,
                        "path": str(child),
                    }
                )
    out.result(
        {"skills": rows, "skills_dir": str(base)},
        human=[f"{row['name']}  ({row['directory']})" for row in rows]
        or ["(no user skills installed)"],
    )
    return 0


def cmd_skill_add(
    out: Out, source: str, name: Optional[str], assume_yes: bool, force: bool
) -> int:
    with tempfile.TemporaryDirectory(prefix="setup-skill-") as tmp:
        tmp_path = Path(tmp)
        if _looks_like_git_url(source):
            result = subprocess.run(
                ["git", "clone", "--depth", "1", source, str(tmp_path / "src")],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise SetupError(f"git clone failed: {result.stderr.strip()[:400]}")
            src = tmp_path / "src"
        else:
            src = Path(source).expanduser()
            if not src.is_dir():
                raise SetupError(
                    f"skill source '{source}' is not a directory or git URL"
                )
        if not (src / "SKILL.md").is_file():
            raise SetupError("not a skill: no SKILL.md at its root")
        skill_name = (name or src.name).strip()
        dest = _skills_dir() / skill_name
        if dest.exists() and not force:
            raise SetupError(f"skill '{skill_name}' already exists (use --force)")
        if not confirm(f"Install skill '{skill_name}'?", assume_yes=assume_yes):
            raise SetupError("aborted", exit_code=2)
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    out.result(
        {"name": skill_name, "installed_to": str(dest)},
        human=f"installed skill '{skill_name}'",
    )
    return 0


def cmd_skill_remove(out: Out, name: str, assume_yes: bool) -> int:
    dest = _skills_dir() / name
    if not dest.is_dir():
        raise SetupError(f"no user skill '{name}'")
    if not confirm(f"Remove skill '{name}'?", assume_yes=assume_yes):
        raise SetupError("aborted", exit_code=2)
    shutil.rmtree(dest)
    out.result({"name": name, "removed": True}, human=f"removed skill '{name}'")
    return 0
