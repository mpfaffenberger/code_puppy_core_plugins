"""Vendored agents become real Code Puppy agents; ``/pstack`` reports status."""

from __future__ import annotations

import pytest
from code_puppy.agents.agent_code_puppy import CodePuppyAgent
from code_puppy.agents.base_agent import BaseAgent
from code_puppy.tools import TOOL_REGISTRY

from code_puppy_core_plugins.pstack.adapter import models_file
from code_puppy_core_plugins.pstack.agents import (
    AGENT_CLASSES,
    CommentSickoAgent,
    PotetoAgent,
    agent_registrations,
)
from code_puppy_core_plugins.pstack.commands import command_help, handle_command
from code_puppy_core_plugins.pstack.upstream import load_pin


def test_registrations_use_frontmatter_names() -> None:
    assert agent_registrations() == [
        {"name": "poteto-agent", "class": PotetoAgent},
        {"name": "comment-sicko", "class": CommentSickoAgent},
    ]


@pytest.mark.parametrize("agent_class", AGENT_CLASSES)
def test_agents_instantiate_with_known_tools(agent_class: type[BaseAgent]) -> None:
    # Skill tools come from the agent_skills plugin, not TOOL_REGISTRY; core's
    # own main agent lists them the same way.
    known = set(TOOL_REGISTRY) | set(CodePuppyAgent().get_available_tools())
    agent = agent_class()
    assert agent.description
    assert set(agent.get_available_tools()) <= known


def test_poteto_agent_prompt_is_translated_and_attributed() -> None:
    text = PotetoAgent().get_system_prompt()
    assert "Vendored from poteto's pstack" in text
    assert "invoke_agent_with_model" in text
    assert "AskQuestion`:" in text  # glossary entry, not a raw instruction
    assert "Fresh subagents by default." not in text  # poteto-mode body not inlined
    assert "poteto-mode" in text


def test_comment_sicko_cannot_create_or_delete_files() -> None:
    tools = CommentSickoAgent().get_available_tools()
    assert "replace_in_file" in tools
    assert "create_file" not in tools
    assert "delete_file" not in tools
    assert "invoke_agent" not in tools


def test_pstack_command_reports_pin_and_model_file() -> None:
    assert command_help() == [
        ("pstack", "poteto's pstack skill pack: status, or `on`/`off`")
    ]
    status = handle_command("/pstack", "pstack")
    assert status is not None
    assert load_pin().commit in status
    assert "absent; run /setup-pstack" in status
    assert "make-bot-ui" in status

    models_file().parent.mkdir(parents=True)
    models_file().write_text("bug-fix: x\n", encoding="utf-8")
    assert "(present)" in (handle_command("/pstack", "pstack") or "")


def test_other_commands_are_not_claimed() -> None:
    assert handle_command("/poteto-mode fix it", "poteto-mode") is None
    assert handle_command("/help", "help") is None
