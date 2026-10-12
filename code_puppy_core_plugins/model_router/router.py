"""Orchestration: config cache, the routing decision, and the decision log.

``Router`` is process-wide state shared by the ``model_select`` hook, the
``agent_run_end`` logger and the ``/router`` command.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from .config import (
    RouterConfig,
    RouterConfigError,
    api_key_for,
    config_path,
    load_config,
    log_path,
)
from .judge_client import JudgeError, JudgeResult, ask_judge
from .policy import (
    REASON_EMPTY_PROMPT,
    REASON_JUDGE_ERROR,
    REASON_NO_API_KEY,
    REASON_UNKNOWN_MODEL,
    Decision,
    decision_from_result,
    fallback_decision,
)
from .state import build_state

logger = logging.getLogger(__name__)

AskFn = Callable[..., JudgeResult]


@dataclass
class PendingRun:
    decision: Decision
    agent_name: str
    started_at: float


class Router:
    def __init__(self, *, ask: AskFn = ask_judge) -> None:
        self._ask = ask
        self._lock = threading.Lock()
        self._config: Optional[RouterConfig] = None
        self._config_mtime: Optional[float] = None
        self._config_error: Optional[str] = None
        self._known_models: Optional[Set[str]] = None
        self._warned: Set[str] = set()
        self._pending: Dict[str, PendingRun] = {}
        self.last_decision: Optional[Decision] = None

    # -- config -------------------------------------------------------------

    def config(self, *, reload: bool = False) -> Optional[RouterConfig]:
        """Cached config, re-read when the file changes. None when absent or invalid."""
        path = config_path()
        try:
            mtime: Optional[float] = path.stat().st_mtime if path.exists() else None
        except OSError:
            mtime = None
        with self._lock:
            if (
                not reload
                and mtime == self._config_mtime
                and (self._config or self._config_error)
            ):
                return self._config
            self._config_mtime = mtime
            self._config = None
            self._config_error = None
            self._known_models = None
            try:
                self._config = load_config(path)
                if self._config is None:
                    self._config_error = f"no config at {path}; run /router init"
            except RouterConfigError as exc:
                self._config_error = str(exc)
            return self._config

    @property
    def config_error(self) -> Optional[str]:
        return self._config_error

    def invalidate(self) -> None:
        with self._lock:
            self._config_mtime = None
            self._config = None
            self._config_error = None
            self._known_models = None
            self._warned.clear()

    # -- model catalogue ----------------------------------------------------

    def known_models(self) -> Set[str]:
        """Names of every model code_puppy can build right now."""
        with self._lock:
            if self._known_models is not None:
                return self._known_models
        try:
            from code_puppy.model_factory import ModelFactory

            names = set(ModelFactory.load_config().keys())
        except Exception:  # pragma: no cover - defensive: catalogue load failures
            logger.debug(
                "model_router: could not load the model catalogue", exc_info=True
            )
            names = set()
        with self._lock:
            self._known_models = names
        return names

    def missing_models(self, cfg: RouterConfig) -> List[str]:
        known = self.known_models()
        if not known:
            return []
        return [name for name in cfg.referenced_models() if name not in known]

    def _warn_once(self, key: str, message: str) -> None:
        with self._lock:
            if key in self._warned:
                return
            self._warned.add(key)
        try:
            from code_puppy.messaging import emit_warning

            emit_warning(message)
        except Exception:  # pragma: no cover - messaging unavailable in tests
            logger.warning(message)

    # -- decisions ----------------------------------------------------------

    def decide(self, prompt: Any, messages: List[Any], cfg: RouterConfig) -> Decision:
        """Judge one turn. Never raises; every failure lands on the fallback model."""
        state = build_state(prompt, messages, cfg.max_request_chars)
        if state is None:
            return fallback_decision(
                cfg, REASON_EMPTY_PROMPT, "no user text to judge -> fallback"
            )

        api_key = api_key_for(cfg)
        if not api_key and cfg.judge.requires_key():
            env = cfg.judge.api_key_env or "the judge API key"
            self._warn_once(
                "no_api_key",
                f"model_router: {env} is not set; routing everything to {cfg.fallback_model}. "
                f"Set it with /set {env.lower()}=<key> or in the environment.",
            )
            return fallback_decision(
                cfg, REASON_NO_API_KEY, f"{cfg.judge.api_key_env} unset -> fallback"
            )

        try:
            result = self._ask(
                state,
                api_key=api_key,
                base_url=cfg.judge.base_url,
                model=cfg.judge.model,
                timeout_s=cfg.judge.timeout_s,
            )
        except JudgeError as exc:
            logger.warning("model_router: judge call failed: %s", exc)
            return fallback_decision(
                cfg, REASON_JUDGE_ERROR, "judge error -> fallback", error=str(exc)
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("model_router: unexpected error", exc_info=True)
            return fallback_decision(
                cfg, REASON_JUDGE_ERROR, "judge error -> fallback", error=repr(exc)
            )

        decision = decision_from_result(result, cfg)
        known = self.known_models()
        if known and decision.model not in known:
            self._warn_once(
                f"unknown:{decision.model}",
                f"model_router: '{decision.model}' is not a registered model; using {cfg.fallback_model}. "
                f"Fix the tiers in {config_path()}.",
            )
            decision.notes.append(
                f"'{decision.model}' unknown -> fallback {cfg.fallback_model}"
            )
            decision.model = cfg.fallback_model
            decision.reason = REASON_UNKNOWN_MODEL
        return decision

    def select(
        self,
        *,
        agent_name: str,
        current_model: Optional[str],
        prompt: Any,
        messages: List[Any],
        session_id: Optional[str],
    ) -> Optional[str]:
        """The ``model_select`` hook body. Returns a model name or None to defer."""
        cfg = self.config()
        if cfg is None or not cfg.enabled or current_model != cfg.virtual_model:
            return None
        decision = self.decide(prompt, messages, cfg)
        self.last_decision = decision
        with self._lock:
            self._pending[session_id or agent_name] = PendingRun(
                decision, agent_name, time.time()
            )
        logger.info("model_router: %s -> %s", agent_name, decision.summary())
        return decision.model

    # -- log ----------------------------------------------------------------

    def record_run_end(
        self,
        *,
        agent_name: str,
        model_name: Optional[str],
        session_id: Optional[str],
        success: bool,
        metadata: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Write one JSONL line for a run this router routed. Returns the entry, or None."""
        with self._lock:
            pending = self._pending.pop(session_id or agent_name, None)
        if pending is None:
            return None
        meta = metadata or {}
        entry: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "agent": agent_name,
            "session_id": session_id,
            "model": model_name or pending.decision.model,
            "success": success,
            "duration_s": round(time.time() - pending.started_at, 2),
            "input_tokens": meta.get("usage_input_tokens"),
            "output_tokens": meta.get("usage_output_tokens"),
            "decision": pending.decision.to_dict(),
        }
        self._append_log(entry)
        return entry

    def _append_log(self, entry: Dict[str, Any]) -> None:
        path: Path = log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            logger.debug("model_router: could not append to %s", path, exc_info=True)

    def read_log(self, limit: int = 200) -> List[Dict[str, Any]]:
        path = log_path()
        if not path.exists():
            return []
        entries: List[Dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            return []
        return entries


ROUTER = Router()
