"""Retirement regression: token_ratio_learner must be fully gone.

Validates installed-package metadata (importlib.metadata entry points),
not just source-tree grep, per the removal brief. Also proves the
*complete* remaining entry-point mapping is unchanged (not just a
representative subset), and that a sample of other plugins actually
import cleanly, not merely remain registered.
"""

import importlib
import importlib.metadata as metadata

import pytest

_GROUP = "code_puppy.plugins"
_DISTRIBUTION = "code-puppy-core-plugins"

# The complete expected code_puppy.plugins mapping after retirement.
# Hardcoded (not derived from pyproject.toml) so a future accidental
# addition, removal, or rename of *any* entry point is caught here,
# not just token_ratio_learner's absence. Update this dict deliberately
# whenever a plugin is added/removed/renamed.
_EXPECTED_ENTRY_POINTS = {
    "acp": "code_puppy_core_plugins.acp.register_callbacks",
    "agent_creator_skill": "code_puppy_core_plugins.agent_creator_skill.register_callbacks",
    "agent_skills": "code_puppy_core_plugins.agent_skills.register_callbacks",
    "attachment_references": "code_puppy_core_plugins.attachment_references.register_callbacks",
    "auto_continue": "code_puppy_core_plugins.auto_continue.register_callbacks",
    "aws_bedrock": "code_puppy_core_plugins.aws_bedrock.register_callbacks",
    "azure_foundry": "code_puppy_core_plugins.azure_foundry.register_callbacks",
    "background_agents": "code_puppy_core_plugins.background_agents.register_callbacks",
    "btw": "code_puppy_core_plugins.btw.register_callbacks",
    "chatgpt_oauth": "code_puppy_core_plugins.chatgpt_oauth.register_callbacks",
    "claude_code_hooks": "code_puppy_core_plugins.claude_code_hooks.register_callbacks",
    "claude_code_oauth": "code_puppy_core_plugins.claude_code_oauth.register_callbacks",
    "code_puppy_agent": "code_puppy_core_plugins.code_puppy_agent.register_callbacks",
    "completion_notification": "code_puppy_core_plugins.completion_notification.register_callbacks",
    "computer_use": "code_puppy_core_plugins.computer_use.register_callbacks",
    "context_indicator": "code_puppy_core_plugins.context_indicator.register_callbacks",
    "copilot_auth": "code_puppy_core_plugins.copilot_auth.register_callbacks",
    "customizable_commands": "code_puppy_core_plugins.customizable_commands.register_callbacks",
    "dbos_durable_exec": "code_puppy_core_plugins.dbos_durable_exec.register_callbacks",
    "destructive_command_guard": "code_puppy_core_plugins.destructive_command_guard.register_callbacks",
    "emoji_filter": "code_puppy_core_plugins.emoji_filter.register_callbacks",
    "empty_html_comment_filter": "code_puppy_core_plugins.empty_html_comment_filter.register_callbacks",
    "example_custom_command": "code_puppy_core_plugins.example_custom_command.register_callbacks",
    "file_permission_handler": "code_puppy_core_plugins.file_permission_handler.register_callbacks",
    "flux_bootstrap": "code_puppy_core_plugins.flux_bootstrap.register_callbacks",
    "force_push_guard": "code_puppy_core_plugins.force_push_guard.register_callbacks",
    "fork": "code_puppy_core_plugins.fork.register_callbacks",
    "frontend_emitter": "code_puppy_core_plugins.frontend_emitter.register_callbacks",
    "grok_oauth": "code_puppy_core_plugins.grok_oauth.register_callbacks",
    "herdr": "code_puppy_core_plugins.herdr.register_callbacks",
    "hook_creator": "code_puppy_core_plugins.hook_creator.register_callbacks",
    "hook_manager": "code_puppy_core_plugins.hook_manager.register_callbacks",
    "jev_grep": "code_puppy_core_plugins.jev_grep.register_callbacks",
    "logfire_oauth": "code_puppy_core_plugins.logfire_oauth.register_callbacks",
    "logfire_sessions": "code_puppy_core_plugins.logfire_sessions.register_callbacks",
    "mcp_binding_prompt": "code_puppy_core_plugins.mcp_binding_prompt.register_callbacks",
    "meta_oauth": "code_puppy_core_plugins.meta_oauth.register_callbacks",
    "namespace_skill_search": "code_puppy_core_plugins.namespace_skill_search.register_callbacks",
    "no_tools": "code_puppy_core_plugins.no_tools.register_callbacks",
    "obsidian_agent": "code_puppy_core_plugins.obsidian_agent.register_callbacks",
    "ollama": "code_puppy_core_plugins.ollama.register_callbacks",
    "ollama_setup": "code_puppy_core_plugins.ollama_setup.register_callbacks",
    "openrouter_oauth": "code_puppy_core_plugins.openrouter_oauth.register_callbacks",
    "plugin_list": "code_puppy_core_plugins.plugin_list.register_callbacks",
    "pop_command": "code_puppy_core_plugins.pop_command.register_callbacks",
    "profiles": "code_puppy_core_plugins.profiles.register_callbacks",
    "prompt_newline": "code_puppy_core_plugins.prompt_newline.register_callbacks",
    "prune": "code_puppy_core_plugins.prune.register_callbacks",
    "puppy_kennel": "code_puppy_core_plugins.puppy_kennel.register_callbacks",
    "puppy_spinner": "code_puppy_core_plugins.puppy_spinner.register_callbacks",
    "qa_kitten_skill": "code_puppy_core_plugins.qa_kitten_skill.register_callbacks",
    "quick_resume": "code_puppy_core_plugins.quick_resume.register_callbacks",
    "review_pr": "code_puppy_core_plugins.review_pr.register_callbacks",
    "session_namer": "code_puppy_core_plugins.session_namer.register_callbacks",
    "spill": "code_puppy_core_plugins.spill.register_callbacks",
    "stack_dump": "code_puppy_core_plugins.stack_dump.register_callbacks",
    "statusline": "code_puppy_core_plugins.statusline.register_callbacks",
    "steer_queue": "code_puppy_core_plugins.steer_queue.register_callbacks",
    "subagent_panel": "code_puppy_core_plugins.subagent_panel.register_callbacks",
    "switch_agent_resume": "code_puppy_core_plugins.switch_agent_resume.register_callbacks",
    "theme": "code_puppy_core_plugins.theme.register_callbacks",
    "timestamp_heartbeat": "code_puppy_core_plugins.timestamp_heartbeat.register_callbacks",
    "universal_constructor": "code_puppy_core_plugins.universal_constructor.register_callbacks",
    "web_retriever_skill": "code_puppy_core_plugins.web_retriever_skill.register_callbacks",
    "wiggum": "code_puppy_core_plugins.wiggum.register_callbacks",
    "yolo_cli": "code_puppy_core_plugins.yolo_cli.register_callbacks",
}

# A sample spanning unrelated plugin families, actually loaded (not just
# checked for registration) to back the PR's "load cleanly" claim.
_LOAD_CHECK_SAMPLE = (
    "herdr",
    "theme",
    "context_indicator",
    "subagent_panel",
    "plugin_list",
    "destructive_command_guard",
)


def _installed_entry_point_names() -> set[str]:
    return {ep.name for ep in metadata.entry_points(group=_GROUP)}


def test_token_ratio_learner_entry_point_is_gone():
    names = _installed_entry_point_names()
    assert "token_ratio_learner" not in names, (
        "token_ratio_learner must not be an installed code_puppy.plugins "
        "entry point after retirement"
    )


def test_token_ratio_learner_package_is_uninstalled():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("code_puppy_core_plugins.token_ratio_learner")


def test_entry_point_mapping_is_exactly_expected():
    """Every surviving registration's name AND target, not a subset.

    A mutation that drops, renames, or retargets *any* other entry point
    must fail this test, not just one that happens to touch a hand-picked
    sample.
    """
    distribution = metadata.distribution(_DISTRIBUTION)
    actual = {
        ep.name: ep.value for ep in distribution.entry_points if ep.group == _GROUP
    }
    assert actual == _EXPECTED_ENTRY_POINTS


def test_survivor_entry_points_actually_load():
    """Actually import() each sampled entry point target, not just check
    that its name is still registered."""
    eps = {ep.name: ep for ep in metadata.entry_points(group=_GROUP)}
    for name in _LOAD_CHECK_SAMPLE:
        module = eps[name].load()
        assert module is not None
