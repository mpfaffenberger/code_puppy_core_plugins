"""Prompt preparation logic for Claude Code OAuth models.

This module owns everything that's special about preparing prompts for
``claude-code-*`` models:

- The fixed system-instruction string Anthropic's Claude Code CLI expects.
- The ``is_claude_code_model`` predicate.
- A callback wired into the ``prepare_model_prompt`` hook which runs inside
  ``code_puppy.model_utils.prepare_prompt_for_model``.

Keeping this here (rather than in core ``model_utils``) keeps the core
model-agnostic and lets the claude-code behavior ship/fail as a single
plugin — exactly per the Contributing guide.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# The OAuth endpoint fingerprints this exact string as the FIRST system block.
# It ships as the agent's standing ``system_prompt`` (its own SystemPromptPart);
# the real system prompt follows as the ``instructions`` block — a separate
# system block, never the user turn. Mirrors CLAUDE_CODE_SYSTEM_PROMPT in
# core's claude_cache_client.
CLAUDE_CODE_INSTRUCTIONS = "You are Claude Code, Anthropic's official CLI for Claude."

# Prefix used by every claude-code-* model (as registered by this plugin).
_CLAUDE_CODE_PREFIX = "claude-code"


def is_claude_code_model(model_name: str) -> bool:
    """Return True if ``model_name`` is a claude-code OAuth model."""
    return model_name.startswith(_CLAUDE_CODE_PREFIX)


def get_claude_code_instructions() -> str:
    """Return the fixed Claude Code system instruction string."""
    return CLAUDE_CODE_INSTRUCTIONS


def prepare_claude_code_prompt(
    model_name: str,
    system_prompt: str,
    user_prompt: str,
    prepend_system_to_user: bool = True,
) -> Optional[Dict[str, Any]]:
    """Callback for the ``prepare_model_prompt`` hook.

    For claude-code models, the Anthropic-expected identity line becomes the
    standing ``system_prompt`` (a separate ``SystemPromptPart``, rendered as
    the first system block) and the caller's system prompt is passed through
    as ``instructions`` (the block right after it). The user prompt is never
    touched.

    ``prepend_system_to_user`` is part of the hook signature core fires with,
    but this plugin never folds the system prompt into the user message.

    Returns ``None`` for non-claude-code models so other handlers / the
    default passthrough can take over.
    """
    if not is_claude_code_model(model_name):
        return None

    return {
        "handled": True,
        "system_prompt": CLAUDE_CODE_INSTRUCTIONS,
        "instructions": system_prompt,
        "user_prompt": user_prompt,
        "is_claude_code": True,
    }
