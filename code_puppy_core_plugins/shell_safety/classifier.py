"""Private shell-command risk classifier."""

from code_puppy.config import get_global_model_name
from code_puppy.private_inference import run_private_prompt
from code_puppy.tools.command_runner import ShellSafetyAssessment

_INSTRUCTIONS = """You are a shell command safety analyzer. Assess risk levels concisely.

Risk levels:
- none: Completely safe (ls, pwd, echo, cat readonly files)
- low: Minimal risk (mkdir, touch, git status, read-only queries)
- medium: Moderate risk (file edits, package installs, service restarts)
- high: Significant risk (rm files, unfiltered UPDATE/DELETE, TRUNCATE, dangerous chmod)
- critical: Severe/destructive (rm -rf, DROP, dd, format, mkfs, mass deletes)

Evaluate scope, reversibility, data-loss potential, privileges, and database
destruction patterns. Return the risk level and at most one sentence of reasoning."""

_NON_THINKING_OVERRIDES = {
    "reasoning_effort": "none",
    "extended_thinking": "off",
    "interleaved_thinking": False,
    "thinking_type": "disabled",
    "thinking_enabled": False,
}


async def classify(command: str, cwd: str | None = None) -> ShellSafetyAssessment:
    """Assess one command without entering Code Puppy's agent runtime."""
    model_name = get_global_model_name()
    if not model_name:
        raise RuntimeError("No model configured for shell-safety assessment")

    prompt = f"Assess this shell command:\n\nCommand: {command}"
    if cwd:
        prompt += f"\nWorking directory: {cwd}"

    return await run_private_prompt(
        model_name=model_name,
        instructions=_INSTRUCTIONS,
        prompt=prompt,
        output_type=ShellSafetyAssessment,
        model_settings_overrides=_NON_THINKING_OVERRIDES,
        max_tokens=128,
    )


__all__ = ["classify"]
