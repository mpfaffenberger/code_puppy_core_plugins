"""Every Cursor primitive a rendered skill still mentions is explained inline."""

from __future__ import annotations

import pytest

from code_puppy_core_plugins.pstack.adapter import FULL_GLOSSARY, glossary, preamble
from code_puppy_core_plugins.pstack.catalog import VendoredSkill, registered_skills
from code_puppy_core_plugins.pstack.register_callbacks import all_skill_registrations

# Cursor vocabulary -> a phrase its glossary entry must contain.
CURSOR_TERMS = {
    "subagent_type": "invoke_agent(agent_name, prompt)",
    "run_in_background": "background=True",
    "readonly": "tell the subagent not to edit files",
    "grok-": "invoke_agent_with_model",
    "AskQuestion": "`ask_user_question`",
    "Bugbot": "use an installed equivalent",
    "cursor-team-kit": "ship with this plugin",
    "deslop": "ship with this plugin",
    "control-ui": "ship with this plugin",
    "agent-transcripts": "autosaves",
    "pstack-models": "/setup-pstack` writes it",
    "/loop": "/goal <predicate>",
}


def _rendered() -> dict[str, str]:
    return {entry["name"]: entry["skill_md"] for entry in all_skill_registrations()}


@pytest.mark.parametrize("skill", registered_skills(), ids=lambda s: s.name)
def test_each_cursor_term_in_a_skill_has_its_glossary_entry(
    skill: VendoredSkill,
) -> None:
    text = _rendered()[skill.name]
    uncovered = [
        term
        for term, explanation in CURSOR_TERMS.items()
        if term in skill.body and explanation not in text
    ]
    assert uncovered == []


def test_glossary_is_trimmed_to_what_a_skill_mentions() -> None:
    assert glossary("plain prose with no editor primitives") == ""
    assert "background=True" in glossary("set run_in_background: true")
    assert "background=True" not in glossary("AskQuestion only")


def test_team_kit_steps_are_no_longer_skipped() -> None:
    text = glossary("Run `/deslop` from `cursor-team-kit`, then `control-ui`.")
    assert "`deslop`, `control-ui`, `control-cli`" in text
    assert "skip" not in text.split("Other Cursor-only extras")[0]


def test_orchestrator_gets_the_full_glossary() -> None:
    assert FULL_GLOSSARY == {"poteto-mode"}
    assert glossary() in _rendered()["poteto-mode"]


def test_preamble_names_the_absolute_source(tmp_path) -> None:
    source = tmp_path / "x" / "SKILL.md"
    text = preamble(source, "")
    assert f"This file is `{source}`" in text
    assert f"against `{source.parent}`" in text
    assert "### Cursor terms" not in text
