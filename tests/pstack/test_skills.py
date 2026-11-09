"""Rendered skills parse with Code Puppy's real loader and resolve their files."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from code_puppy_core_plugins.agent_skills.metadata import (
    load_full_skill_content,
    parse_skill_metadata,
)

from code_puppy_core_plugins.pstack.adapter import translate
from code_puppy_core_plugins.pstack.catalog import (
    COMPANION_SKILLS,
    CORE_NAMESPACE,
    NATIVE_SKILLS,
    PRINCIPLES_INDEX_NAME,
    SKILL_NAMESPACES,
    namespace,
    principles,
    registered_skills,
)
from code_puppy_core_plugins.pstack.register_callbacks import all_skill_registrations
from code_puppy_core_plugins.pstack.upstream import (
    PSTACK,
    SKILLS_DIR,
    TEAM_KIT,
    split_frontmatter,
)

# Relative links a skill body tells the agent to open.
_RELATIVE_LINK = re.compile(r"\]\(((?:\.\./|references/|playbooks/)[^)#\s]+)")


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    return {entry["name"]: entry["skill_md"] for entry in all_skill_registrations()}


def _materialize(tmp_path: Path, name: str, text: str) -> Path:
    directory = tmp_path / name
    directory.mkdir()
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    return directory


def test_registers_each_mapped_skill_plus_the_principles_index(
    rendered: dict[str, str],
) -> None:
    expected = [
        *SKILL_NAMESPACES,
        *COMPANION_SKILLS,
        *NATIVE_SKILLS,
        PRINCIPLES_INDEX_NAME,
    ]
    assert sorted(rendered) == sorted(expected)
    assert len(rendered) == 33


def test_every_skill_parses_with_the_agent_skills_loader(
    rendered: dict[str, str], tmp_path: Path
) -> None:
    authors = {skill.name: skill.origin.author for skill in registered_skills()}
    for name, text in rendered.items():
        metadata = parse_skill_metadata(_materialize(tmp_path, name, text))
        assert metadata is not None, name
        assert metadata.name == name
        is_index = name == PRINCIPLES_INDEX_NAME
        assert metadata.tags[0] == (CORE_NAMESPACE if is_index else namespace(name))
        assert CORE_NAMESPACE in metadata.tags
        assert metadata.author == authors.get(name, PSTACK.author)


def test_descriptions_survive_rendering_verbatim(
    rendered: dict[str, str], tmp_path: Path
) -> None:
    for skill in registered_skills():
        upstream, _ = split_frontmatter(skill.skill_md.read_text("utf-8"))
        metadata = parse_skill_metadata(
            _materialize(tmp_path, skill.name, rendered[skill.name])
        )
        assert metadata is not None
        assert metadata.description == " ".join(upstream["description"].split())


def test_listing_cost_stays_small(rendered: dict[str, str], tmp_path: Path) -> None:
    # The skill catalogue lists name + description; bodies load on demand.
    listing = 0
    for name, text in rendered.items():
        metadata = parse_skill_metadata(_materialize(tmp_path, name, text))
        assert metadata is not None
        listing += len(name) + len(metadata.description)
    assert listing < 12_000


def test_body_loads_in_full_and_anchors_relative_paths(
    rendered: dict[str, str], tmp_path: Path
) -> None:
    directory = _materialize(tmp_path, "how", rendered["how"])
    content = load_full_skill_content(directory)
    assert content is not None
    assert f"This file is `{SKILLS_DIR / 'how' / 'SKILL.md'}`" in content
    assert "# How" in content


def test_team_kit_skills_credit_cursor(rendered: dict[str, str]) -> None:
    for name in COMPANION_SKILLS:
        text = rendered[name]
        assert "Vendored from Cursor's cursor-team-kit" in text
        assert f"`{TEAM_KIT.skills_dir / name / 'SKILL.md'}`" in text
    assert "Vendored from poteto's pstack" in rendered["how"]


def test_every_relative_link_resolves_inside_the_vendor_tree() -> None:
    missing = [
        f"{skill.name}: {link}"
        for skill in registered_skills()
        for link in _RELATIVE_LINK.findall(skill.body)
        if not (skill.directory / link).exists()
    ]
    assert missing == []


def test_principles_index_points_at_every_leaf(rendered: dict[str, str]) -> None:
    index = rendered[PRINCIPLES_INDEX_NAME]
    leaves = principles()
    assert len(leaves) == 24
    for leaf in leaves:
        assert f"**{leaf.name}**" in index
        assert f"`{leaf.skill_md}`" in index
        assert leaf.skill_md.is_file()
    # Leaves are pointers only; their bodies load when the agent reads them.
    assert leaves[0].body[200:400] not in index


@pytest.mark.parametrize(
    "cursor_text",
    [
        "AskQuestion",
        "the Task tool",
        "~/.cursor/rules/pstack-models.mdc",
        ".cursor/skills/",
    ],
)
def test_translate_removes_cursor_names_with_direct_equivalents(
    cursor_text: str,
) -> None:
    corpus = "\n".join(skill.body for skill in registered_skills())
    assert cursor_text in corpus, "fixture drifted: pick a term upstream still uses"
    assert cursor_text not in translate(corpus)


def test_translate_maps_to_code_puppy_tools() -> None:
    assert translate("Prefer AskQuestion over free text") == (
        "Prefer `ask_user_question` over free text"
    )
    assert translate("via the Task tool with") == "via `invoke_agent` with"
