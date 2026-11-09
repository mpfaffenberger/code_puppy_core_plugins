"""Which vendored pstack skills Code Puppy registers, and under which namespace.

Every upstream skill directory must land in exactly one bucket: a namespace
in ``SKILL_NAMESPACES``, an entry in ``EXCLUDED_SKILLS``, or the
``principle-`` family that collapses into the ``pstack-principles`` index.
``tests/test_vendor_integrity.py`` enforces that, so a refresh that adds a
skill fails loudly until someone decides where it goes.

``COMPANION_SKILLS`` are the ``cursor-team-kit`` skills pstack invokes by name
(``/deslop``, ``control-ui``, ``control-cli``), vendored from the same pin so
those steps run instead of being skipped. ``NATIVE_SKILLS`` are written for
this plugin (under ``code_puppy_core_plugins.pstack/skills/``) to stand in for Cursor
built-ins pstack calls by name, such as ``create-skill``.

Namespaces become the skill's first tag, which ``namespace_skill_search``
uses to list the whole pack as a handful of one-line groups instead of one
line per skill.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

from code_puppy_core_plugins.pstack.upstream import (
    NATIVE,
    PSTACK,
    SKILLS_DIR,
    TEAM_KIT,
    Origin,
    split_frontmatter,
)

PRINCIPLE_PREFIX = "principle-"
PRINCIPLES_INDEX_NAME = "pstack-principles"
CORE_NAMESPACE = "pstack"

SKILL_NAMESPACES: dict[str, str] = {
    "poteto-mode": CORE_NAMESPACE,
    "poteto-help": CORE_NAMESPACE,
    "setup-pstack": CORE_NAMESPACE,
    "automate-me": CORE_NAMESPACE,
    "correct": CORE_NAMESPACE,
    "reflect": CORE_NAMESPACE,
    "how": "pstack-understand",
    "why": "pstack-understand",
    "recall": "pstack-understand",
    "teach": "pstack-understand",
    "in-your-own-words": "pstack-understand",
    "ketchup": "pstack-understand",
    "bro": "pstack-understand",
    "architect": "pstack-design",
    "arena": "pstack-design",
    "swarm": "pstack-design",
    "figure-it-out": "pstack-design",
    "tdd": "pstack-verify",
    "interrogate": "pstack-verify",
    "blast-radius": "pstack-verify",
    "benchmark-checklist": "pstack-verify",
    "create-verification-skill": "pstack-verify",
    "maintain-verification-skill": "pstack-verify",
    "show-me-your-work": "pstack-verify",
    "unslop": "pstack-write",
    "technical-writing": "pstack-write",
    "no-comments": "pstack-write",
    "typescript-best-practices": "pstack-write",
}

COMPANION_SKILLS: dict[str, str] = {
    "deslop": "pstack-write",
    "control-ui": "pstack-verify",
    "control-cli": "pstack-verify",
}

NATIVE_SKILLS: dict[str, str] = {
    "create-skill": "pstack-write",
}

_NAMESPACES = {**SKILL_NAMESPACES, **COMPANION_SKILLS, **NATIVE_SKILLS}

EXCLUDED_SKILLS: dict[str, str] = {
    "make-bot-ui": (
        "Drives Grok Bot webhook routines through Cursor's update_state tool; "
        "Code Puppy has no equivalent surface."
    ),
}


@dataclass(frozen=True)
class VendoredSkill:
    """One upstream ``skills/<name>/SKILL.md`` with its parsed frontmatter."""

    name: str
    description: str
    body: str
    directory: Path
    origin: Origin

    @property
    def skill_md(self) -> Path:
        return self.directory / "SKILL.md"


def _read(directory: Path, origin: Origin = PSTACK) -> VendoredSkill:
    fields, body = split_frontmatter(
        (directory / "SKILL.md").read_text(encoding="utf-8")
    )
    return VendoredSkill(
        # Registration keys on the directory name: upstream frontmatter names
        # drift ("Poteto Mode"), but every cross-reference uses the dir name.
        name=directory.name,
        description=" ".join(fields.get("description", "").split()),
        body=body,
        directory=directory,
        origin=origin,
    )


def vendored_skill_names(origin: Origin = PSTACK) -> list[str]:
    """Every skill directory vendored from ``origin``, registered or not."""
    return sorted(
        path.name
        for path in origin.skills_dir.iterdir()
        if (path / "SKILL.md").is_file()
    )


def namespace(name: str) -> str:
    """The namespace a registered skill is listed under."""
    return _NAMESPACES[name]


@cache
def registered_skills() -> tuple[VendoredSkill, ...]:
    """Skills exposed one-to-one: pstack's, its team-kit companions, natives."""
    sources = (
        (PSTACK, SKILL_NAMESPACES),
        (TEAM_KIT, COMPANION_SKILLS),
        (NATIVE, NATIVE_SKILLS),
    )
    return tuple(
        _read(origin.skills_dir / name, origin)
        for origin, names in sources
        for name in names
    )


@cache
def principles() -> tuple[VendoredSkill, ...]:
    """The ``principle-*`` leaves, read lazily through the index skill."""
    return tuple(
        _read(SKILLS_DIR / name)
        for name in vendored_skill_names()
        if name.startswith(PRINCIPLE_PREFIX)
    )


def namespace_counts() -> dict[str, int]:
    """Registered skill count per namespace, including the principles index."""
    counts: dict[str, int] = {}
    for group in [*_NAMESPACES.values(), CORE_NAMESPACE]:
        counts[group] = counts.get(group, 0) + 1
    return counts
