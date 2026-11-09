"""Translate vendored pstack markdown into Code Puppy skills and prompts.

Vendored files stay byte-identical to upstream. Everything Cursor-specific is
handled here at render time, in two layers:

1. ``REWRITES``: exact-substring swaps for paths and tool names whose Code
   Puppy equivalent is unambiguous.
2. A glossary prepended to every rendered skill and agent prompt for the
   primitives that need judgment (subagent spawns, per-call models, todo
   lists, transcripts, Cursor-only products).

Bundled files are never copied into the skill cache, so each preamble names
the absolute vendored directory that relative paths resolve against.
"""

from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from code_puppy import config as cp_config
from code_puppy_core_plugins.pstack.catalog import (
    COMPANION_SKILLS,
    CORE_NAMESPACE,
    PRINCIPLES_INDEX_NAME,
    VendoredSkill,
    namespace,
    principles,
)
from code_puppy_core_plugins.pstack.upstream import PSTACK, SKILLS_DIR, Origin, load_pin

MODELS_FILE_NAME = "pstack-models.md"
# The orchestrator reads playbooks and sibling skills raw, so it gets every
# glossary entry rather than only those its own body triggers.
FULL_GLOSSARY = frozenset({"poteto-mode"})
USER_SKILLS_DIR = "~/.code_puppy/skills/"
PROJECT_SKILLS_DIR = ".code_puppy/skills/"


def models_file() -> Path:
    """Code Puppy's stand-in for Cursor's always-applied ``pstack-models.mdc``."""
    return Path(cp_config.CONFIG_DIR) / "pstack" / MODELS_FILE_NAME


def _rewrites() -> tuple[tuple[str, str], ...]:
    # Longest match first: shorter keys are suffixes of longer ones.
    return (
        ("~/.cursor/rules/pstack-models.mdc", str(models_file())),
        ("pstack-models.mdc", MODELS_FILE_NAME),
        ("~/.cursor/skills/", USER_SKILLS_DIR),
        (".cursor/skills/", PROJECT_SKILLS_DIR),
        ("the Task tool", "`invoke_agent`"),
        ("`AskQuestion`", "`ask_user_question`"),
        ("AskQuestion", "`ask_user_question`"),
        # Cursor's built-in create-skill is replaced by this plugin's own.
        ("Cursor's built-in `create-skill` skill", "the `create-skill` skill"),
        ("Cursor's built-in `create-skill`", "the `create-skill` skill"),
        (" (Cursor's built-in for authoring SKILL.md files)", ""),
        (
            "`/loop` and `/create-skill` are Cursor built-ins.",
            "`/loop` is a Cursor built-in; `/create-skill` ships with this plugin.",
        ),
        # Native skills run plugin modules with Code Puppy's own interpreter.
        ("$CODE_PUPPY_PYTHON", sys.executable),
    )


def translate(text: str) -> str:
    """Apply the unambiguous Cursor-to-Code-Puppy substring rewrites."""
    for old, new in _rewrites():
        text = text.replace(old, new)
    return text


@dataclass(frozen=True)
class GlossaryEntry:
    """One Cursor primitive, the body substrings that mention it, its mapping."""

    triggers: tuple[str, ...]
    text: str


def _entries() -> tuple[GlossaryEntry, ...]:
    sessions = Path(cp_config.DATA_DIR) / "subagent_sessions"
    return (
        GlossaryEntry(
            ("Task", "subagent", "spawn"),
            "Spawning a subagent (a `Task` call): `invoke_agent(agent_name, "
            'prompt)`. `subagent_type: "poteto-agent"` means `agent_name='
            '"poteto-agent"`; `generalPurpose` means `agent_name="code-puppy"`. '
            '"In one message" means start the calls together (in code mode, '
            "`asyncio.gather` in one `run_code`).",
        ),
        GlossaryEntry(
            ("`model`", "grok-", "claude-", "inherit-parent"),
            "A per-spawn `model`: pick the closest alias from "
            "`list_available_models()` and call `invoke_agent_with_model("
            "agent_name, prompt, model_name)`. Use plain `invoke_agent` for "
            "`inherit-parent`, `auto`, a missing role line, or when no alias "
            "matches or that tool is unavailable.",
        ),
        GlossaryEntry(
            ("run_in_background", "readonly", "environment", "cloud"),
            "`run_in_background: true`: `invoke_agent(..., background=True)`. "
            "`readonly: true` has no flag; tell the subagent not to edit files. "
            '`environment: "cloud"` runs locally; give each parallel writer its '
            "own `git worktree`.",
        ),
        GlossaryEntry(
            ("AskQuestion", "`Read`", "Glob", "Shell"),
            "`AskQuestion`: `ask_user_question`. Read, Grep, Glob, Shell: "
            "`read_file`, `grep`, `list_files`, `shell`.",
        ),
        GlossaryEntry(
            ("pstack-models", "setup-pstack"),
            f"The `pstack-models.mdc` rule is `{models_file()}`; it is injected "
            "into the system prompt when present, and `/setup-pstack` writes it.",
        ),
        GlossaryEntry(
            (".cursor/skills", "create-skill"),
            f"Skill dirs `.cursor/skills/` and `~/.cursor/skills/` are "
            f"`{PROJECT_SKILLS_DIR}` and `{USER_SKILLS_DIR}`. Author or edit "
            "skills there with the bundled `create-skill` skill.",
        ),
        GlossaryEntry(
            ("disable-model-invocation",),
            "Code Puppy has no `disable-model-invocation`; every enabled skill "
            "is listed. Make the description trigger only on the skill's name "
            "or `/<name>` instead.",
        ),
        GlossaryEntry(
            ("principle",),
            "Principle skills are not registered one by one. "
            f'`activate_skill("{PRINCIPLES_INDEX_NAME}")` lists them; each leaf '
            f"is `{SKILLS_DIR}/principle-<name>/SKILL.md`.",
        ),
        GlossaryEntry(
            ("transcript",),
            "Chat transcripts (`agent-transcripts/`): "
            f"`{cp_config.AUTOSAVE_DIR}` and `{sessions}`.",
        ),
        GlossaryEntry(
            ("todo", "Todo"),
            "The todo list: a checklist you keep current in your replies, or in "
            "a scratch markdown file on long runs.",
        ),
        GlossaryEntry(
            ("/loop",),
            "`/loop`: `/goal <predicate>` when the wiggum plugin is installed; "
            "otherwise keep working in-turn and poll with `shell`.",
        ),
        GlossaryEntry(
            ("cursor-team-kit", "deslop", "control-"),
            "The `cursor-team-kit` skills pstack calls ship with this plugin: "
            + ", ".join(f"`{name}`" for name in COMPANION_SKILLS)
            + ". Load them with `activate_skill` (or `/deslop`).",
        ),
        GlossaryEntry(
            ("Bugbot", "bugbot", "cursor-team-kit", "verify-this", "Cursor"),
            "Other Cursor-only extras (Bugbot, cloud agents, team-kit skills "
            "not listed above such as `verify-this`): use an installed "
            "equivalent if `list_or_search_skills` finds one; otherwise skip "
            "that step and say so.",
        ),
    )


def glossary(body: str | None = None) -> str:
    """Glossary bullets for the primitives ``body`` mentions (all if ``None``)."""
    bullets = [
        f"- {entry.text}"
        for entry in _entries()
        if body is None or any(trigger in body for trigger in entry.triggers)
    ]
    if not bullets:
        return ""
    return "### Cursor terms, read as Code Puppy\n" + "\n".join(bullets)


def attribution(origin: Origin = PSTACK) -> str:
    if not origin.vendored:
        return (
            "> **pstack for Code Puppy.** Written for this plugin to stand in "
            "for a Cursor built-in that pstack calls."
        )
    pin = load_pin()
    return (
        f"> **pstack for Code Puppy.** Vendored from {origin.title} "
        f"({pin.repository}/tree/{pin.short_commit}/{origin.path}, MIT)."
    )


def preamble(source: Path, body: str | None = None, origin: Origin = PSTACK) -> str:
    """Attribution, path anchor, and the glossary ``body`` needs."""
    anchor = (
        f" This file is `{source}`. Resolve its relative paths (`references/`, "
        f"`playbooks/`, `scripts/`, `../<skill>/SKILL.md`) against "
        f"`{source.parent}`, and read those files only when a step needs them."
    )
    terms = glossary(body)
    return (
        attribution(origin) + anchor + (f"\n\n{terms}" if terms else "") + "\n\n---\n\n"
    )


def _frontmatter(
    name: str, description: str, group: str, author: str = PSTACK.author
) -> str:
    folded = textwrap.fill(
        description,
        width=78,
        initial_indent="  ",
        subsequent_indent="  ",
        break_on_hyphens=False,
        break_long_words=False,
    )
    tags = [group] if group == CORE_NAMESPACE else [group, CORE_NAMESPACE]
    tag_lines = "\n".join(f"  - {tag}" for tag in tags)
    return (
        f"---\nname: {name}\ndescription: >-\n{folded}\ntags:\n{tag_lines}\n"
        f"author: {author}\nversion: {load_pin().version}\n---\n\n"
    )


def render_skill(skill: VendoredSkill) -> str:
    """Full ``SKILL.md`` text Code Puppy materializes for a vendored skill."""
    body = None if skill.name in FULL_GLOSSARY else skill.body
    return (
        _frontmatter(
            skill.name, skill.description, namespace(skill.name), skill.origin.author
        )
        + preamble(skill.skill_md, body, skill.origin)
        + translate(skill.body)
    )


def principles_index_description() -> str:
    return (
        f"Index of pstack's {len(principles())} one-rule engineering principles "
        "(laziness protocol, fix root causes, prove it works, model the domain, "
        "and more). Use when poteto-mode or the user names a principle, or "
        "before a design, debugging, or verification call."
    )


def render_principles_index() -> str:
    """Generated skill that lists principle leaves for on-demand reading."""
    entries = "\n".join(
        f"- **{leaf.name}**: {leaf.description} `{leaf.skill_md}`"
        for leaf in principles()
    )
    body = (
        "# pstack principles\n\n"
        "One-rule principles from poteto's pstack; `poteto-mode` cites them in "
        "its Principles section. Pick the leaves whose trigger matches the "
        "decision in front of you, read each in full with `read_file` before "
        "you apply or cite it, and name the choice it changed.\n\n"
        f"{entries}\n"
    )
    return (
        _frontmatter(
            PRINCIPLES_INDEX_NAME, principles_index_description(), CORE_NAMESPACE
        )
        + attribution()
        + "\n\n"
        + body
    )
