"""Register the bundled ``agent-creator`` delegation skill."""

from pathlib import Path

from code_puppy.callbacks import register_callback

_SKILL_DIR = Path(__file__).resolve().parent


def _register_agent_creator_skill() -> list[dict]:
    return [
        {
            "name": "agent-creator",
            "skill_md_path": str(_SKILL_DIR / "SKILL.md"),
        }
    ]


register_callback("register_skills", _register_agent_creator_skill)
