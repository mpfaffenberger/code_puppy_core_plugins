"""The routing prompt: Code Puppy vocabulary only, and only while enabled."""

from __future__ import annotations

import importlib
import re
import sys
from importlib.metadata import entry_points

import pytest
from code_puppy import callbacks
from code_puppy.plugins import _plugin_loading_context
from code_puppy.plugins.config import set_plugin_disabled
from code_puppy.tools import TOOL_REGISTRY

from code_puppy_core_plugins.pstack import prompt
from code_puppy_core_plugins.pstack.adapter import models_file
from code_puppy_core_plugins.pstack.catalog import (
    PRINCIPLES_INDEX_NAME,
    SKILL_NAMESPACES,
)

# Cursor primitives that have no meaning to a Code Puppy agent.
CURSOR_ONLY = (
    "Cursor",
    ".cursor",
    "subagent_type",
    "Task tool",
    "`Task`",
    "AskQuestion",
    "run_in_background",
    "readonly",
    "Bugbot",
    "bugbot",
    ".mdc",
    "grok-",
    "mcp_auth",
    "Await",
    "cursor-team-kit",
)
PLUGIN_NAME = "pstack"


def test_routing_prompt_has_no_cursor_only_vocabulary() -> None:
    text = prompt.routing_prompt()
    assert [term for term in CURSOR_ONLY if term in text] == []


def test_routing_prompt_only_names_real_tools_and_skills() -> None:
    text = prompt.ROUTING
    mentioned = set(re.findall(r"`([a-z_]+)`", text))
    tools = mentioned & set(TOOL_REGISTRY)
    assert tools == {
        "invoke_agent",
        "invoke_agent_with_model",
        "list_available_models",
        "ask_user_question",
        "shell",
    }
    skills = set(re.findall(r"`([a-z][a-z-]+)`", text)) - mentioned - {"poteto-agent"}
    assert skills <= {*SKILL_NAMESPACES, PRINCIPLES_INDEX_NAME}
    for required in ("poteto-mode", "how", "architect", "tdd", PRINCIPLES_INDEX_NAME):
        assert f"`{required}`" in text


def test_sub_agents_are_told_not_to_restart_the_playbook() -> None:
    """load_prompt reaches delegates too; they must not fan out again."""
    assert prompt.SUBAGENT_RULE in prompt.ROUTING
    assert "`poteto-mode`" in prompt.SUBAGENT_RULE
    # The rule must follow the routing and delegation lines it overrides.
    assert prompt.ROUTING.index(prompt.SUBAGENT_RULE) > prompt.ROUTING.index(
        "Delegate with `invoke_agent`"
    )


def test_models_file_is_appended_when_present() -> None:
    assert prompt.routing_prompt() == prompt.ROUTING
    path = models_file()
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\ndescription: roles\n---\n# budget: large\nbug-fix: opus-x\n",
        encoding="utf-8",
    )
    text = prompt.routing_prompt()
    assert text.startswith(prompt.ROUTING)
    assert text.endswith(
        f"### pstack model roles (from `{path}`)\n# budget: large\nbug-fix: opus-x"
    )


def test_models_file_is_capped(pstack_on: None) -> None:
    path = models_file()
    path.parent.mkdir(parents=True)
    path.write_text("x" * (prompt.MODELS_FILE_MAX_CHARS * 3), encoding="utf-8")
    added = prompt.routing_prompt()[len(prompt.ROUTING) :]
    assert added.count("x") == prompt.MODELS_FILE_MAX_CHARS


def test_entry_point_is_named_after_the_disable_key() -> None:
    points = entry_points(group="code_puppy.plugins", name=PLUGIN_NAME)
    assert [point.value for point in points] == [
        "code_puppy_core_plugins.pstack.register_callbacks"
    ]


MODULE_NAME = "code_puppy_core_plugins.pstack.register_callbacks"
HOOKS = (
    ("register_skills", "skill_registrations"),
    ("load_prompt", "prompt_addition"),
    ("register_agents", "agents"),
    ("register_agent_tools", "agent_tools"),
    ("custom_command_help", "command_help"),
    ("custom_command", "handle_command"),
)


def _unhook() -> None:
    module = sys.modules.pop(MODULE_NAME, None)
    for phase, attribute in HOOKS if module else ():
        callbacks.unregister_callback(phase, getattr(module, attribute))


@pytest.fixture
def loaded_plugin():
    """Import register_callbacks the way the plugin loader does, then unhook.

    Other test modules import it without a loading context, and registration
    dedupes by function, so drop those unowned hooks first.
    """
    _unhook()
    with _plugin_loading_context(PLUGIN_NAME):
        module = importlib.import_module(MODULE_NAME)
    yield module
    _unhook()


def test_prompt_injected_only_while_plugin_enabled(loaded_plugin) -> None:
    assert callbacks.get_callback_owner(loaded_plugin.prompt_addition) == PLUGIN_NAME
    assert prompt.ROUTING in callbacks.on_load_prompt()

    set_plugin_disabled(PLUGIN_NAME, True)
    assert prompt.ROUTING not in callbacks.on_load_prompt()
    skill_names = {
        entry["name"]
        for result in callbacks.on_register_skills()
        for entry in (result or [])
    }
    assert "poteto-mode" not in skill_names
    assert loaded_plugin.agent_tools not in callbacks.get_callbacks(
        "register_agent_tools"
    )

    set_plugin_disabled(PLUGIN_NAME, False)
    assert prompt.ROUTING in callbacks.on_load_prompt()


def test_model_routing_tools_only_go_to_the_main_agent(loaded_plugin) -> None:
    assert loaded_plugin.agent_tools("code-puppy") == [
        "list_available_models",
        "invoke_agent_with_model",
    ]
    assert loaded_plugin.agent_tools("qa-kitten") == []
    assert loaded_plugin.agent_tools(None) == []
