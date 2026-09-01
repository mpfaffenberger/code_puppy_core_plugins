"""Tests for the claude-code ``prepare_model_prompt`` hook.

The OAuth endpoint fingerprints the identity line as the FIRST system block.
It ships as the agent's standing ``system_prompt`` (its own SystemPromptPart);
the real system prompt follows as ``instructions`` — a separate system block,
never smuggled into the user turn.
"""

import pytest

from code_puppy_core_plugins.claude_code_oauth.prompt_handler import (
    CLAUDE_CODE_INSTRUCTIONS,
    is_claude_code_model,
    prepare_claude_code_prompt,
)

SYSTEM = "You are Biscuit, a very good dog."
USER = "fetch me a PR"


@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        ("claude-code-fable-5.1", True),
        ("claude-code-opus-4-7", True),
        ("claude-opus-4-7", False),
        ("gpt-5.6", False),
    ],
)
def test_is_claude_code_model(model_name, expected):
    assert is_claude_code_model(model_name) is expected


def test_non_claude_code_model_is_ignored():
    assert prepare_claude_code_prompt("gpt-5.6", SYSTEM, USER) is None


@pytest.mark.parametrize("prepend_system_to_user", [True, False])
def test_identity_is_standing_system_prompt_and_real_prompt_is_instructions(
    prepend_system_to_user,
):
    result = prepare_claude_code_prompt(
        "claude-code-fable-5.1",
        SYSTEM,
        USER,
        prepend_system_to_user=prepend_system_to_user,
    )

    assert result["handled"] is True
    assert result["is_claude_code"] is True
    # Identity line leads as its own SystemPromptPart ...
    assert result["system_prompt"] == CLAUDE_CODE_INSTRUCTIONS
    # ... the real prompt follows as the instructions block, untouched ...
    assert result["instructions"] == SYSTEM
    # ... and the user turn is never touched, regardless of the legacy flag.
    assert result["user_prompt"] == USER
