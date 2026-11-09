"""Wire pstack into Code Puppy, dark until ``/pstack on``.

Two switches, both honored at dispatch time with no restart:

- the ``pstack`` setting (default off) gates skills, routing prompt, agents
  and tools; ``/pstack`` always answers so users can find and enable it;
- ``/plugins`` (``disabled_plugins``) still turns the whole plugin off,
  ``/pstack`` included.
"""

from __future__ import annotations

from code_puppy.callbacks import register_callback

from code_puppy_core_plugins.plugin_settings import register_settings
from code_puppy_core_plugins.pstack.adapter import (
    render_principles_index,
    render_skill,
)
from code_puppy_core_plugins.pstack.agents import agent_registrations
from code_puppy_core_plugins.pstack.catalog import (
    PRINCIPLES_INDEX_NAME,
    registered_skills,
)
from code_puppy_core_plugins.pstack.commands import command_help, handle_command
from code_puppy_core_plugins.pstack.config import SETTINGS, is_enabled
from code_puppy_core_plugins.pstack.prompt import routing_prompt

# pstack routes reviewers and delegates to specific models per role; the main
# agent needs the per-call override tools to honor that.
MODEL_ROUTING_TOOLS = ["list_available_models", "invoke_agent_with_model"]
MODEL_ROUTING_AGENTS = frozenset({"code-puppy"})


def all_skill_registrations() -> list[dict[str, str]]:
    """Every rendered SKILL.md, regardless of the ``pstack`` setting."""
    entries = [
        {"name": skill.name, "skill_md": render_skill(skill)}
        for skill in registered_skills()
    ]
    entries.append(
        {"name": PRINCIPLES_INDEX_NAME, "skill_md": render_principles_index()}
    )
    return entries


def skill_registrations() -> list[dict[str, str]]:
    """``register_skills`` payload: empty while pstack is off."""
    return all_skill_registrations() if is_enabled() else []


def prompt_addition() -> str | None:
    """``load_prompt``: the routing rules, only while pstack is on."""
    return routing_prompt() if is_enabled() else None


def agents() -> list[dict[str, object]]:
    return agent_registrations() if is_enabled() else []


def agent_tools(agent_name: str | None = None) -> list[str]:
    """``register_agent_tools``: model-routing tools for the main agent only."""
    if not is_enabled() or agent_name not in MODEL_ROUTING_AGENTS:
        return []
    return MODEL_ROUTING_TOOLS


register_callback("register_skills", skill_registrations)
register_callback("load_prompt", prompt_addition)
register_callback("register_agents", agents)
register_callback("register_agent_tools", agent_tools)
register_callback("custom_command_help", command_help)
register_callback("custom_command", handle_command)
register_settings(SETTINGS)
