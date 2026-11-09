"""``/pstack [on|off]``: status, and the switch for the whole pack.

Per-skill commands (``/poteto-mode``, ``/how``, ...) come for free from the
agent_skills plugin, which exposes every enabled skill as ``/<name>``.
"""

from __future__ import annotations

from code_puppy_core_plugins.pstack.adapter import models_file
from code_puppy_core_plugins.pstack.catalog import (
    COMPANION_SKILLS,
    EXCLUDED_SKILLS,
    namespace_counts,
    principles,
)
from code_puppy_core_plugins.pstack.config import ENABLED_KEY, is_enabled, set_enabled
from code_puppy_core_plugins.pstack.upstream import TEAM_KIT, VENDOR_DIR, load_pin

COMMAND = "pstack"
USAGE = "Usage: /pstack [on|off]"


def command_help() -> list[tuple[str, str]]:
    return [(COMMAND, "poteto's pstack skill pack: status, or `on`/`off`")]


def _disabled() -> str:
    pin = load_pin()
    return "\n".join(
        [
            f"pstack {pin.version} (poteto's skill pack) is off.",
            "It adds workflow skills (/poteto-mode, /how, /architect, /tdd, "
            "/deslop, /create-skill, ...), 24 engineering principles, two "
            "agents and a short routing prompt.",
            f"Turn it on with /pstack on (or /set {ENABLED_KEY}=true).",
        ]
    )


def _status() -> str:
    pin = load_pin()
    groups = ", ".join(f"{ns} ({n})" for ns, n in sorted(namespace_counts().items()))
    models = models_file()
    model_state = "present" if models.is_file() else "absent; run /setup-pstack"
    skipped = ", ".join(f"{name} ({why})" for name, why in EXCLUDED_SKILLS.items())
    return "\n".join(
        [
            f"pstack {pin.version} by poteto (Lauren Tan), MIT",
            f"Pinned: {pin.repository} @ {pin.commit}",
            f"Vendored at: {VENDOR_DIR}",
            f"Skill groups: {groups}",
            f"From {TEAM_KIT.path} (MIT, Cursor): {', '.join(COMPANION_SKILLS)}",
            f"Principles: {len(principles())} leaves via pstack-principles",
            f"Model roles: {models} ({model_state})",
            f"Not registered: {skipped}",
            "Start with: /poteto-mode <task>. Unsure which skill fits? /poteto-help",
        ]
    )


def handle_command(command: str, name: str) -> str | None:
    """Status or toggle; ``None`` for commands this plugin does not own."""
    if name != COMMAND:
        return None
    match command.split()[1:]:
        case []:
            return _status() if is_enabled() else _disabled()
        case ["on"]:
            set_enabled(True)
            return "pstack is on.\n" + _status()
        case ["off"]:
            set_enabled(False)
            return "pstack is off. Its skills, agents and prompt are gone."
        case _:
            return USAGE
