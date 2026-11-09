"""The always-on routing block pstack adds to every agent's system prompt.

It spends a few hundred tokens on *when* to reach for which skill; the skills
themselves load on demand. It must stay free of Cursor vocabulary, which is
enforced by ``tests/test_prompt.py``.
"""

from __future__ import annotations

import logging

from code_puppy_core_plugins.pstack.adapter import models_file
from code_puppy_core_plugins.pstack.catalog import PRINCIPLES_INDEX_NAME
from code_puppy_core_plugins.pstack.upstream import SKILLS_DIR, split_frontmatter

logger = logging.getLogger(__name__)

# A user-edited file lands in every system prompt; cap what it can cost.
MODELS_FILE_MAX_CHARS = 4000

# load_prompt reaches sub-agents too, and core gives this hook no agent name.
# Without this rule a delegate handed one slice of a task would restart the
# poteto-mode playbook and fan out again.
SUBAGENT_RULE = (
    "Running as a sub-agent: skip the `poteto-mode` routing and the delegation "
    "rule above; do the task you were given, loading a specific skill only "
    "when it serves that task."
)

ROUTING = f"""\
## pstack (poteto's engineering skills)
Load a skill with `activate_skill(name)` (or read `{SKILLS_DIR}/<name>/SKILL.md`).
- Multi-file change, a design or architecture choice, a bug with an unknown \
cause, or a perf issue: activate `poteto-mode` and follow its playbook.
- Specific intent goes straight to its skill: `how` (how X works, where code \
should live), `why` (history and intent), `architect` (types and module shape \
before code), `tdd` (test first), `interrogate` (adversarial multi-model \
review), `blast-radius` (what a change could break), `arena` or `swarm` \
(parallel candidates or fan-out), `unslop` or `technical-writing` (prose), \
`{PRINCIPLES_INDEX_NAME}` (principles; read only the leaves you apply).
- Verify on the real artifact before calling work done; \
`create-verification-skill` builds a reusable driver for this project.
- Small contained work (one file with an obvious test, a question, a one-line \
edit): do it directly, then verify.
- Delegate with `invoke_agent` (`poteto-agent` for playbook delegates). When a \
role names a model, use `invoke_agent_with_model` with an alias from \
`list_available_models`. Ask with `ask_user_question`; run with `shell`.
- {SUBAGENT_RULE}
- User and project instructions win over pstack."""


def _models_block() -> str | None:
    path = models_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("pstack: cannot read %s: %s", path, exc)
        return None
    _, body = split_frontmatter(text)
    body = body.strip()[:MODELS_FILE_MAX_CHARS]
    if not body:
        return None
    return f"### pstack model roles (from `{path}`)\n{body}"


def routing_prompt() -> str:
    """``load_prompt`` fragment: routing rules plus the user's model roles."""
    models = _models_block()
    return ROUTING if models is None else f"{ROUTING}\n\n{models}"
