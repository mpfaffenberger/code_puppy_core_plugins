"""The bundled create-skill replaces Cursor's built-in, and its validator works."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from code_puppy_core_plugins.pstack.catalog import registered_skills
from code_puppy_core_plugins.pstack.register_callbacks import all_skill_registrations
from code_puppy_core_plugins.pstack.skill_validator import main, validate
from code_puppy_core_plugins.pstack.upstream import NATIVE

CREATE_SKILL_DIR = NATIVE.skills_dir / "create-skill"
GOOD = """---
name: {name}
description: >-
  Does a thing. Use for "do the thing".
---

# Thing

Read [the details](references/details.md) when needed.
"""


def _skill(root: Path, name: str, text: str, with_reference: bool = True) -> Path:
    directory = root / name
    (directory / "references").mkdir(parents=True)
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    if with_reference:
        (directory / "references" / "details.md").write_text("x", encoding="utf-8")
    return directory


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    return {entry["name"]: entry["skill_md"] for entry in all_skill_registrations()}


def test_create_skill_is_registered_and_credited(rendered: dict[str, str]) -> None:
    text = rendered["create-skill"]
    assert "author: Code Puppy" in text
    assert "stand in for a Cursor built-in" in text
    assert f"{sys.executable} -m code_puppy_core_plugins.pstack.skill_validator" in text
    assert "$CODE_PUPPY_PYTHON" not in text


@pytest.mark.parametrize(
    "section",
    [
        "YAML rules",
        "writing guidelines",
        "Draft / test / iterate loop",
        "Description-optimization loop",
    ],
)
def test_create_skill_has_every_section_pstack_calls(section: str) -> None:
    # reflect and automate-me hand off to these sections by name.
    assert section in (CREATE_SKILL_DIR / "SKILL.md").read_text("utf-8")


def test_no_rendered_skill_still_calls_create_skill_a_cursor_builtin(
    rendered: dict[str, str],
) -> None:
    leftovers = [
        name
        for name, text in rendered.items()
        if "built-in `create-skill`" in text
        or "Cursor's built-in for authoring" in text
        or "`/create-skill` are Cursor built-ins" in text
    ]
    assert leftovers == []
    callers = [s.name for s in registered_skills() if "create-skill" in s.body]
    assert {"reflect", "automate-me", "poteto-mode"} <= set(callers)


def test_bundled_skill_passes_its_own_validator() -> None:
    report = validate(CREATE_SKILL_DIR)
    assert (report.errors, report.warnings) == ([], [])


@pytest.mark.parametrize("clause", ["Use for", "Use when asked for", "Use only when"])
def test_validator_accepts_a_good_skill(tmp_path: Path, clause: str) -> None:
    text = GOOD.format(name="do-thing").replace("Use for", clause)
    report = validate(_skill(tmp_path, "do-thing", text))
    assert (report.errors, report.warnings) == ([], [])


def test_validator_reports_each_breakage(tmp_path: Path) -> None:
    directory = _skill(
        tmp_path,
        "Bad_Name",
        GOOD.format(name="other").replace("Use for", "For") + "\n.cursor/skills/\n",
        with_reference=False,
    )
    report = validate(directory)
    assert len(report.errors) == 2  # name differs from dir, dead link
    assert any("does not exist" in error for error in report.errors)
    assert len(report.warnings) == 2  # no trigger clause, Cursor path


def test_validator_rejects_non_kebab_names(tmp_path: Path) -> None:
    directory = _skill(tmp_path, "Do_Thing", GOOD.format(name="Do_Thing"))
    assert [e for e in validate(directory).errors if "kebab-case" in e]


def test_validator_rejects_missing_frontmatter(tmp_path: Path) -> None:
    directory = _skill(tmp_path, "plain", "# no frontmatter\n")
    assert validate(directory).errors


def test_validator_cli_exit_codes(tmp_path: Path) -> None:
    good = _skill(tmp_path, "do-thing", GOOD.format(name="do-thing"))
    assert main([str(good)]) == 0
    assert main([str(tmp_path / "missing")]) == 1
    # The exact command the skill tells the agent to run.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "code_puppy_core_plugins.pstack.skill_validator",
            str(good),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("OK ")
