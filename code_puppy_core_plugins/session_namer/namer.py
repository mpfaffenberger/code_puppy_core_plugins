"""One-shot model calls that name autosave sessions for the /resume browser.

Writes ``title`` / ``subtitle`` / ``tags`` into the session's metadata
sidecar -- the exact keys the two-pane session browser already reads --
plus ``ai_named_at`` (the message count at naming time) so re-naming is
**incremental**: the previous summary and only the messages since the
last naming are fed back to the model, letting the title evolve
gracefully instead of being regenerated from scratch.

Follows the ``btw`` side-query pattern: a throwaway history-free
pydantic_ai Agent on a worker thread with its own event loop. All work
funnels through a single-worker executor so a burst of autosaves can
never stampede the model.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

QUERY_TIMEOUT_S = 60.0
# Re-name after this many new messages accumulate since the last naming.
RENAME_DELTA = 16
# Max sessions to backfill per browser open (cost control).
BACKFILL_LIMIT = 10
_DIGEST_CHAR_BUDGET = 2400
_SNIPPET_CHARS = 240
_MAX_TAGS = 4

_INSTRUCTIONS = (
    "You name coding-agent chat sessions for a resume picker. Given a "
    "digest of the conversation (and possibly the previous summary), "
    "reply with ONLY a JSON object: "
    '{"title": <=8 words naming the main task, '
    '"subtitle": <=12 words of concrete detail, '
    '"tags": up to 4 lowercase single-word topic tags}. '
    "When a previous summary is provided, refine it: keep the title "
    "stable unless the session's focus materially changed. "
    "No prose, no code fences -- just the JSON object."
)

_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="session-namer"
)
_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def naming_needed(meta: dict) -> bool:
    """True when a session has never been AI-named or grew enough since."""
    named_at = meta.get("ai_named_at")
    if not isinstance(named_at, int):
        return True
    try:
        count = int(meta.get("message_count", 0))
    except (TypeError, ValueError):
        return False
    return count - named_at >= RENAME_DELTA


def build_digest(history: list, since: int = 0) -> str:
    """Compact plain-text digest of user/assistant turns from ``since``."""
    lines: list[str] = []
    used = 0
    for msg in history[since:]:
        for part in getattr(msg, "parts", ()) or ():
            kind = getattr(part, "part_kind", "")
            if kind not in ("user-prompt", "text"):
                continue
            content = getattr(part, "content", None)
            if not isinstance(content, str) or not content.strip():
                continue
            role = "user" if kind == "user-prompt" else "assistant"
            snippet = content.strip()[:_SNIPPET_CHARS]
            line = f"{role}: {snippet}"
            if used + len(line) > _DIGEST_CHAR_BUDGET:
                return "\n".join(lines)
            lines.append(line)
            used += len(line)
    return "\n".join(lines)


def build_prompt(digest: str, previous: Optional[dict]) -> str:
    if previous:
        prior = {
            "title": previous.get("title", ""),
            "subtitle": previous.get("subtitle", ""),
            "tags": previous.get("tags", []),
        }
        return (
            f"Previous summary:\n{json.dumps(prior)}\n\n"
            f"Conversation since then:\n{digest}"
        )
    return f"Conversation digest:\n{digest}"


def parse_naming(raw: str) -> Optional[dict]:
    """Tolerantly extract ``{title, subtitle, tags}`` from model output."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    subtitle = data.get("subtitle")
    tags = data.get("tags")
    clean_tags = []
    if isinstance(tags, list):
        for tag in tags:
            if len(clean_tags) >= _MAX_TAGS:
                break
            if isinstance(tag, str) and tag.strip():
                clean_tags.append(tag.strip().lstrip("#").lower())
    return {
        "title": title.strip(),
        "subtitle": subtitle.strip() if isinstance(subtitle, str) else "",
        "tags": clean_tags,
    }


async def _ask(model_name: str, prompt: str) -> str:
    """Single-turn, history-free naming query. Raises on failure."""
    from pydantic_ai import Agent, UsageLimits

    from code_puppy.model_factory import ModelFactory, make_model_settings
    from code_puppy.model_utils import prepare_prompt_for_model

    models_config = ModelFactory.load_config()
    if model_name not in models_config:
        raise ValueError(f"model {model_name!r} not present in the model config")

    model = ModelFactory.get_model(model_name, models_config)
    prepared = prepare_prompt_for_model(
        model_name,
        _INSTRUCTIONS,
        prompt,
        prepend_system_to_user=True,
    )
    agent = Agent(
        model=model,
        instructions=prepared.instructions,
        retries=1,
        model_settings=make_model_settings(model_name),
    )
    result = await agent.run(
        prepared.user_prompt,
        usage_limits=UsageLimits(request_limit=2),
    )
    return str(result.output)


def resolve_model_name() -> Optional[str]:
    """Configured override first, else the ``btw`` current-model logic."""
    try:
        from code_puppy.config import get_value

        override = str(get_value("session_namer_model") or "").strip()
        if override:
            return override
    except Exception:
        pass
    from ..btw.side_query import resolve_model_name as btw_resolve

    return btw_resolve()


def name_session(
    base_dir: Path, session_name: str, live_meta: Optional[dict] = None
) -> bool:
    """Name (or incrementally re-name) one session. Returns True on write.

    Runs on the namer worker thread. ``live_meta`` is the browser's
    in-memory metadata dict; updating it in place surfaces the new
    title/tags on the next repaint without a disk re-read.
    """
    try:
        from code_puppy.command_line.session_browser_data import (
            _get_session_metadata,
            merge_sidecar,
        )
        from code_puppy.session_storage import load_session

        meta = _get_session_metadata(base_dir, session_name)
        if not naming_needed(meta):
            return False
        history = load_session(session_name, base_dir)
        named_at = meta.get("ai_named_at")
        since = named_at if isinstance(named_at, int) else 0
        previous = meta if isinstance(named_at, int) else None
        digest = build_digest(history, since)
        if not digest.strip():
            # Nothing nameable yet; snooze until the session grows.
            merge_sidecar(base_dir, session_name, {"ai_named_at": len(history)})
            return False

        model_name = resolve_model_name()
        if not model_name:
            return False
        raw = asyncio.run(
            asyncio.wait_for(
                _ask(model_name, build_prompt(digest, previous)),
                timeout=QUERY_TIMEOUT_S,
            )
        )
        naming = parse_naming(raw)
        if naming is None:
            logger.debug("session_namer: unparseable output for %s", session_name)
            return False

        updates = {**naming, "ai_named_at": len(history)}
        if not merge_sidecar(base_dir, session_name, updates):
            return False
        if live_meta is not None:
            live_meta.update(updates)
        return True
    except Exception:
        logger.debug("session_namer: naming %s failed", session_name, exc_info=True)
        return False


def submit(base_dir: Path, session_name: str, live_meta: Optional[dict] = None) -> bool:
    """Queue a naming job unless one is already in flight for the session."""
    with _inflight_lock:
        if session_name in _inflight:
            return False
        _inflight.add(session_name)

    def job() -> None:
        try:
            name_session(base_dir, session_name, live_meta)
        finally:
            with _inflight_lock:
                _inflight.discard(session_name)

    try:
        _executor.submit(job)
        return True
    except Exception:  # pragma: no cover - interpreter shutdown
        with _inflight_lock:
            _inflight.discard(session_name)
        return False
