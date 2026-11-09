"""Read-only access to the vendored cursor/plugins subset and its pin manifest.

The vendor dir mirrors the upstream repository layout: ``pstack/`` plus the
few ``cursor-team-kit/`` skills pstack calls. Manifest keys are paths relative
to the vendor dir, which equal paths relative to the upstream repository.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = PACKAGE_DIR / "vendor"
MANIFEST_PATH = VENDOR_DIR / "UPSTREAM.json"


@dataclass(frozen=True)
class Origin:
    """Where a skill set lives and who to credit for it."""

    path: str
    title: str
    author: str
    base: Path = VENDOR_DIR

    @property
    def vendored(self) -> bool:
        return self.base == VENDOR_DIR

    @property
    def root(self) -> Path:
        return self.base / self.path

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"


PSTACK = Origin("pstack", "poteto's pstack", "poteto (Lauren Tan)")
TEAM_KIT = Origin("cursor-team-kit", "Cursor's cursor-team-kit", "Cursor")
# Skills this plugin writes itself, for Cursor built-ins that pstack calls.
NATIVE = Origin(".", "pstack for Code Puppy", "Code Puppy", PACKAGE_DIR)

PSTACK_DIR = PSTACK.root
SKILLS_DIR = PSTACK.skills_dir
AGENTS_DIR = PSTACK_DIR / "agents"

_BLOCK_SCALARS = frozenset({">", ">-", ">+", "|", "|-", "|+"})


@dataclass(frozen=True)
class UpstreamPin:
    """The upstream commit the vendor tree was copied from."""

    repository: str
    commit: str
    version: str
    files: dict[str, str]

    @property
    def short_commit(self) -> str:
        return self.commit[:12]


@cache
def load_pin() -> UpstreamPin:
    """Parse ``UPSTREAM.json``, written by ``scripts/refresh_upstream.py``."""
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return UpstreamPin(
        repository=data["repository"],
        commit=data["commit"],
        version=data["version"],
        files=dict(data["files"]),
    )


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
        return decoded if isinstance(decoded, str) else value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _join_block(lines: list[str], style: str) -> str:
    stripped = [line.strip() for line in lines]
    if style.startswith("|"):
        return "\n".join(stripped).strip()
    return " ".join(part for part in stripped if part)


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a markdown file into top-level frontmatter scalars and body.

    Covers the YAML subset upstream pstack uses: plain, single-quoted, and
    double-quoted scalars plus folded or literal block scalars. Nested keys
    and lists are ignored because nothing here reads them.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return {}, text

    fields: dict[str, str] = {}
    index = 1
    while index < end:
        line = lines[index]
        index += 1
        if not line.strip() or line[0].isspace() or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        value = raw.strip()
        if value in _BLOCK_SCALARS:
            block: list[str] = []
            while index < end and (
                not lines[index].strip() or lines[index][0].isspace()
            ):
                block.append(lines[index])
                index += 1
            fields[key.strip()] = _join_block(block, value)
        else:
            fields[key.strip()] = _unquote(value)
    return fields, "\n".join(lines[end + 1 :]).lstrip("\n")
