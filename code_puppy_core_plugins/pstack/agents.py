"""Code Puppy agents built from pstack's vendored ``agents/*.md`` files."""

from __future__ import annotations

from functools import cache
from pathlib import Path

from code_puppy.agents.base_agent import BaseAgent
from code_puppy_core_plugins.pstack.adapter import preamble, translate
from code_puppy_core_plugins.pstack.upstream import AGENTS_DIR, split_frontmatter

_READ_TOOLS = ["list_files", "read_file", "grep", "shell"]
_SKILL_TOOLS = ["activate_skill", "list_or_search_skills"]
_EDIT_TOOLS = ["create_file", "replace_in_file", "delete_snippet", "delete_file"]


@cache
def _parsed(path: Path) -> tuple[dict[str, str], str]:
    return split_frontmatter(path.read_text(encoding="utf-8"))


class VendoredAgent(BaseAgent):
    """An agent whose identity and prompt come from one vendored markdown file."""

    SOURCE: Path
    DISPLAY_NAME: str
    TOOLS: tuple[str, ...]

    @property
    def name(self) -> str:
        return _parsed(self.SOURCE)[0]["name"]

    @property
    def display_name(self) -> str:
        return self.DISPLAY_NAME

    @property
    def description(self) -> str:
        return _parsed(self.SOURCE)[0]["description"]

    def get_available_tools(self) -> list[str]:
        return list(self.TOOLS)

    def get_system_prompt(self) -> str:
        # Delegates read playbooks and sibling skills raw, so they get every
        # glossary entry, not just the ones their own prompt mentions.
        return preamble(self.SOURCE) + translate(_parsed(self.SOURCE)[1])


class PotetoAgent(VendoredAgent):
    """poteto-mode's delegate for code-writing and helper steps."""

    SOURCE = AGENTS_DIR / "poteto-agent.md"
    DISPLAY_NAME = "Poteto Agent"
    TOOLS = (
        "list_agents",
        "invoke_agent",
        "list_available_models",
        "invoke_agent_with_model",
        *_READ_TOOLS,
        *_EDIT_TOOLS,
        "ask_user_question",
        *_SKILL_TOOLS,
        "load_image_for_analysis",
    )


class CommentSickoAgent(VendoredAgent):
    """The ``no-comments`` skill's reviewer: deletes comments, flags workarounds."""

    SOURCE = AGENTS_DIR / "comment-sicko.md"
    DISPLAY_NAME = "Comment Sicko"
    TOOLS = (*_READ_TOOLS, "replace_in_file", "delete_snippet", *_SKILL_TOOLS)


AGENT_CLASSES: tuple[type[VendoredAgent], ...] = (PotetoAgent, CommentSickoAgent)


def agent_registrations() -> list[dict[str, object]]:
    """``register_agents`` payload, keyed by each file's frontmatter name."""
    return [
        {"name": _parsed(cls.SOURCE)[0]["name"], "class": cls} for cls in AGENT_CLASSES
    ]
