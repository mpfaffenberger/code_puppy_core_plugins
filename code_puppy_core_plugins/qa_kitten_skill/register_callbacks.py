"""Register the bundled ``qa-kitten`` delegation skill."""

from pathlib import Path

from code_puppy.callbacks import register_callback

_SKILL_DIR = Path(__file__).resolve().parent


def _register_qa_kitten_skill() -> list[dict]:
    return [
        {
            "name": "qa-kitten",
            "skill_md_path": str(_SKILL_DIR / "SKILL.md"),
        }
    ]


register_callback("register_skills", _register_qa_kitten_skill)
