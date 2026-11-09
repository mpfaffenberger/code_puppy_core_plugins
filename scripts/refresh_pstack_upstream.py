#!/usr/bin/env python3
"""Re-vendor pstack from cursor/plugins at a pinned commit.

Usage (from the repository root)::

    python scripts/refresh_pstack_upstream.py --commit <sha>
    python scripts/refresh_pstack_upstream.py --source /path/to/plugins

Without ``--commit`` the commit already recorded in ``UPSTREAM.json`` is
re-fetched, which makes the script a cheap "is the vendor tree pristine?"
check. ``--source`` copies from a local checkout of the whole cursor/plugins
repository instead of fetching (pass ``--commit`` too, to record it).

Two upstream directories are vendored from the same commit: ``pstack/`` and
the ``cursor-team-kit`` skills pstack calls by name (``TEAM_KIT_SKILLS``).
The vendor tree mirrors the upstream layout, is replaced wholesale, and
``UPSTREAM.json`` records the commit plus a SHA-256 per file.
``tests/pstack/test_vendor_integrity.py`` fails until every vendored skill is
classified in ``catalog.py``, so a refresh that adds or removes a skill cannot
slip through silently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY = "https://github.com/cursor/plugins"
PSTACK = "pstack"
TEAM_KIT = "cursor-team-kit"
UPSTREAM_SUBDIRS = (PSTACK, TEAM_KIT)
# Only the team-kit skills pstack's own files invoke (`/deslop`, the control
# skills). Must match ``catalog.COMPANION_SKILLS``; a test checks.
TEAM_KIT_SKILLS = ("control-cli", "control-ui", "deslop")
VENDOR_DIR = (
    Path(__file__).resolve().parents[1]
    / "code_puppy_core_plugins"
    / "pstack"
    / "vendor"
)
MANIFEST = VENDOR_DIR / "UPSTREAM.json"

_PSTACK_TREES = ("skills/", "agents/", "automations/")
_PSTACK_FILES = frozenset({"LICENSE", "README.md", "plugin.json"})
_GUIDE_PREFIX = "docs/guide/"
_SKIPPED_PARTS = frozenset({"node_modules", ".DS_Store", "__pycache__"})


def _is_pstack_file(relative: str) -> bool:
    if relative in _PSTACK_FILES or relative.startswith(_PSTACK_TREES):
        return True
    # The guide is prose the help skill links to; its images are 2+ MB of
    # screenshots no agent reads, so only the markdown ships.
    return relative.startswith(_GUIDE_PREFIX) and relative.endswith(".md")


def _is_team_kit_file(relative: str) -> bool:
    if relative == "LICENSE":
        return True
    return relative.startswith(tuple(f"skills/{name}/" for name in TEAM_KIT_SKILLS))


def is_vendored(relative: str) -> bool:
    """Return whether a cursor/plugins repo-relative path belongs in vendor."""
    if _SKIPPED_PARTS.intersection(relative.split("/")):
        return False
    subdir, _, inner = relative.partition("/")
    if subdir == PSTACK:
        return _is_pstack_file(inner)
    if subdir == TEAM_KIT:
        return _is_team_kit_file(inner)
    return False


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fetch(commit: str, workdir: Path) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=workdir, check=True)

    git("init", "-q")
    git("remote", "add", "origin", f"{REPOSITORY}.git")
    git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", commit)
    git("sparse-checkout", "set", *UPSTREAM_SUBDIRS)
    git("checkout", "-q", "FETCH_HEAD")
    return workdir


def _copy_tree(source: Path) -> dict[str, str]:
    """Copy the vendored subset of a cursor/plugins checkout; hash each file."""
    hashes: dict[str, str] = {}
    for subdir in UPSTREAM_SUBDIRS:
        if (VENDOR_DIR / subdir).exists():
            shutil.rmtree(VENDOR_DIR / subdir)
        for path in sorted((source / subdir).rglob("*")):
            relative = path.relative_to(source).as_posix()
            if not path.is_file() or not is_vendored(relative):
                continue
            target = VENDOR_DIR / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            hashes[relative] = _sha256(target)
    return hashes


def _recorded_commit() -> str | None:
    if not MANIFEST.is_file():
        return None
    commit = json.loads(MANIFEST.read_text(encoding="utf-8")).get("commit")
    return commit if isinstance(commit, str) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--commit", help="cursor/plugins commit SHA to vendor")
    parser.add_argument(
        "--source", type=Path, help="local cursor/plugins checkout (skips fetching)"
    )
    args = parser.parse_args(argv)

    commit = args.commit or _recorded_commit()
    if not commit:
        parser.error("--commit is required when UPSTREAM.json has no commit")

    with tempfile.TemporaryDirectory() as tmp:
        source = args.source or _fetch(commit, Path(tmp))
        plugin_json = source / PSTACK / "plugin.json"
        version = json.loads(plugin_json.read_text(encoding="utf-8"))["version"]
        hashes = _copy_tree(source)

    manifest = {
        "repository": REPOSITORY,
        "paths": list(UPSTREAM_SUBDIRS),
        "commit": commit,
        "version": version,
        "files": hashes,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Vendored pstack {version} @ {commit}: {len(hashes)} files")
    for subdir in UPSTREAM_SUBDIRS:
        prefix = f"{subdir}/skills/"
        skills = sorted({rel.split("/")[2] for rel in hashes if rel.startswith(prefix)})
        print(f"{subdir}: {len(skills)} skills: {', '.join(skills)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
