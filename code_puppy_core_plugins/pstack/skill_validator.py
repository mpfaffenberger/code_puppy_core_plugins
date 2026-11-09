"""Validate a Code Puppy skill directory the way the skill loader reads it.

Usage::

    python -m code_puppy_core_plugins.pstack.skill_validator <skill-dir> [<skill-dir> ...]

Errors (exit 1) are things that break loading or linking. Warnings are style
limits the ``create-skill`` skill asks for. Frontmatter goes through the real
``agent_skills`` parser, so "valid" here means "Code Puppy will load it".
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from code_puppy_core_plugins.agent_skills.metadata import parse_skill_metadata

NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1024
MAX_SKILL_LINES = 500
# Markdown link targets that are local files (not URLs, anchors, or mail).
_LOCAL_LINK = re.compile(r"\]\((?!https?:|mailto:|#)([^)#\s]+)")
_CURSOR_PATHS = (".cursor/", "~/.cursor")
# "Use for ...", "Use when ...", "Use only when ...".
_TRIGGER_CLAUSE = re.compile(r"\buse\b(?:\s+\w+){0,2}?\s+(?:when|for)\b", re.I)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate(directory: Path) -> Report:
    """Check one skill directory; never raises for a malformed skill."""
    report = Report()
    skill_md = directory / "SKILL.md"
    if not skill_md.is_file():
        report.errors.append(f"{skill_md} does not exist")
        return report

    try:
        metadata = parse_skill_metadata(directory)
    except Exception as exc:  # the loader's own error is the useful message
        report.errors.append(f"frontmatter does not parse: {exc}")
        return report
    if metadata is None:
        report.errors.append("frontmatter does not parse (missing name or ---)")
        return report

    name = metadata.name
    if name != directory.name:
        report.errors.append(f"name {name!r} differs from directory {directory.name!r}")
    if not NAME_PATTERN.match(name) or len(name) > MAX_NAME_CHARS:
        report.errors.append(f"name {name!r} is not kebab-case <= {MAX_NAME_CHARS}")

    description = " ".join(metadata.description.split())
    if len(description) > MAX_DESCRIPTION_CHARS:
        report.errors.append(
            f"description is {len(description)} chars (max {MAX_DESCRIPTION_CHARS})"
        )
    if not _TRIGGER_CLAUSE.search(description):
        report.warnings.append("description has no 'Use when/for ...' trigger clause")

    text = skill_md.read_text(encoding="utf-8")
    for target in sorted(set(_LOCAL_LINK.findall(text))):
        if not (directory / target).exists():
            report.errors.append(f"link target {target!r} does not exist")
    for leftover in _CURSOR_PATHS:
        if leftover in text:
            report.warnings.append(f"mentions {leftover!r}; use .code_puppy/skills/")
    if (lines := text.count("\n") + 1) > MAX_SKILL_LINES:
        report.warnings.append(
            f"SKILL.md is {lines} lines; move detail into references/"
        )
    return report


def main(argv: list[str] | None = None) -> int:
    targets = [Path(arg).expanduser().resolve() for arg in (argv or sys.argv[1:])]
    if not targets:
        print(__doc__)
        return 2
    failed = False
    for target in targets:
        report = validate(target)
        failed |= bool(report.errors)
        status = "FAIL" if report.errors else "OK"
        print(f"{status} {target}")
        for line in report.errors:
            print(f"  error: {line}")
        for line in report.warnings:
            print(f"  warning: {line}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
