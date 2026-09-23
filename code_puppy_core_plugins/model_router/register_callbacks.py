"""Model router: route each turn to the cheapest capable model.

Select the virtual model (``/model auto``) and every turn is judged by
TypeSafe Jev on two questions, how demanding the request is and whether a
mistake would be costly, then sent to the matching model from your tiers.
The tool loop of that turn stays on the chosen model. Any other model in
``/model`` switches routing off.

Hooks:
- ``model_select``: the decision, once per run, before the agent is built
- ``load_models_config`` / ``register_model_type``: the ``auto`` entry,
  which builds as the fallback model when the hook did not run
- ``agent_run_end``: appends the decision and token usage to
  ``<STATE_DIR>/model_routes.jsonl``
- ``/router``: status, models, init, route (dry run), last, log, on, off, reload,
  with tab completion for subcommands, ``init`` flags and model names

Config lives in ``<DATA_DIR>/model_router.json``; see ``config.py``.
"""

from __future__ import annotations

import logging
import shlex
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from code_puppy.callbacks import register_callback
from code_puppy.messaging import emit_error, emit_info, emit_success, emit_warning

from .config import (
    RouterConfigError,
    api_key_for,
    config_path,
    log_path,
    save_config,
    template_config,
)
from .policy import REASON_NO_API_KEY
from .router import ROUTER

logger = logging.getLogger(__name__)

MODEL_TYPE = "model_router"
DEFAULT_CONTEXT_LENGTH = 128000

USAGE = """Usage:
  /router                  status: config, key, last decision
  /router models           registered model names you can use as tiers
  /router init [--flagship M] [--mid M] [--cheap M] [--force]
                        write a starting config (defaults every tier to the current model)
  /router route <prompt>   dry run: ask the judge and show the decision without running anything
  /router last             the last decision in full
  /router log [n]          recent routed runs with counts per model and reason
  /router on | off         toggle routing without editing the file
  /router reload           re-read the config file"""


# ---------------------------------------------------------------------------
# Model registration: the virtual model and its type
# ---------------------------------------------------------------------------


def _load_models() -> Dict[str, Any]:
    """Expose the virtual model so it appears in /model."""
    cfg = ROUTER.config()
    if cfg is None:
        return {}
    return {
        cfg.virtual_model: {
            "type": MODEL_TYPE,
            "name": cfg.virtual_model,
            "context_length": cfg.context_length or DEFAULT_CONTEXT_LENGTH,
            "description": "Judge-routed: cheapest capable model per turn",
        }
    }


def _create_virtual_model(model_name: str, model_config: Dict, config: Dict) -> Any:
    """Build the virtual model as its fallback.

    Reached only when an agent is built without the ``model_select`` hook
    having chosen a real model for the run, so the fallback is the safe choice.
    """
    from code_puppy.model_factory import ModelFactory

    cfg = ROUTER.config()
    if cfg is None:
        emit_warning(f"model_router: {ROUTER.config_error}")
        return None
    if cfg.fallback_model == model_name:
        emit_warning("model_router: fallback_model cannot be the virtual model itself")
        return None
    return ModelFactory.get_model(cfg.fallback_model, config)


def _register_model_types() -> List[Dict[str, Any]]:
    return [{"type": MODEL_TYPE, "handler": _create_virtual_model}]


# ---------------------------------------------------------------------------
# Per-run routing and logging
# ---------------------------------------------------------------------------


def _model_select(
    *,
    agent_name: str,
    current_model: Optional[str],
    prompt: Any,
    messages: List[Any],
    session_id: Optional[str] = None,
) -> Optional[str]:
    return ROUTER.select(
        agent_name=agent_name,
        current_model=current_model,
        prompt=prompt,
        messages=messages,
        session_id=session_id,
    )


async def _agent_run_end(
    agent_name: str,
    model_name: Optional[str],
    session_id: Optional[str] = None,
    success: bool = True,
    error: Optional[Exception] = None,
    response_text: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    ROUTER.record_run_end(
        agent_name=agent_name,
        model_name=model_name,
        session_id=session_id,
        success=success,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# /router command
# ---------------------------------------------------------------------------


def _completion_provider():
    from .completer import RouterCompleter

    return RouterCompleter()


def _custom_help() -> List[Tuple[str, str]]:
    return [
        (
            "router",
            "Model router: status, models, init, route <prompt>, last, log, on, off",
        )
    ]


def _status() -> None:
    cfg = ROUTER.config(reload=True)
    if cfg is None:
        emit_warning(f"model_router: {ROUTER.config_error}")
        emit_info(USAGE)
        return
    lines = [
        f"config:      {config_path()}",
        f"enabled:     {'yes' if cfg.enabled else 'no'}",
        f"virtual:     {cfg.virtual_model}  (select it with /model {cfg.virtual_model})",
        f"tiers:       {' < '.join(cfg.tiers)}",
        f"levels:      {', '.join(f'{i}={m}' for i, m in enumerate(cfg.complexity_to_tier))}",
        f"fallback:    {cfg.fallback_model}  (confidence < {cfg.confidence_threshold}, low_confidence={cfg.low_confidence})",
    ]
    if cfg.risk_floor:
        lines.append(
            f"risk floor:  risky >= {cfg.risk_floor.threshold} -> at least {cfg.risk_floor.min_tier}"
        )
    lines.append(
        f"judge:       {cfg.judge.model} at {cfg.judge.base_url}, timeout {cfg.judge.timeout_s}s"
    )
    if cfg.judge.api_key_env:
        lines.append(
            f"api key:     {cfg.judge.api_key_env} {'set' if api_key_for(cfg) else 'NOT SET'}"
        )
    else:
        lines.append("api key:     none (judge.api_key_env unset)")
    missing = ROUTER.missing_models(cfg)
    if missing:
        lines.append(
            f"unknown models: {', '.join(missing)}  (not in /model; fix the config)"
        )
    if ROUTER.last_decision:
        lines.append(f"last:        {ROUTER.last_decision.summary()}")
    lines.append(f"log:         {log_path()}")
    emit_info("\n".join(lines))


def _models() -> None:
    """List the model names usable as tiers, grouped by type."""
    try:
        from code_puppy.model_factory import ModelFactory

        catalogue = ModelFactory.load_config()
    except Exception as exc:
        emit_error(f"model_router: could not load the model catalogue: {exc}")
        return
    by_type: Dict[str, List[str]] = {}
    for name, entry in sorted(catalogue.items()):
        model_type = (
            str((entry or {}).get("type", "?")) if isinstance(entry, dict) else "?"
        )
        if model_type == MODEL_TYPE:
            continue
        by_type.setdefault(model_type, []).append(name)
    if not by_type:
        emit_info("model_router: no models registered")
        return
    lines = ["registered models (use these names in tiers):"]
    for model_type, names in sorted(by_type.items()):
        lines.append(f"  [{model_type}]")
        lines.extend(f"    {name}" for name in names)
    lines.append("example: /router init --flagship <name> --mid <name> --cheap <name>")
    emit_info("\n".join(lines))


def _parse_flags(args: List[str]) -> Dict[str, Any]:
    flags: Dict[str, Any] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--force":
            flags["force"] = True
        elif arg in ("--flagship", "--mid", "--cheap") and i + 1 < len(args):
            flags[arg[2:]] = args[i + 1]
            i += 1
        else:
            raise ValueError(f"unexpected argument '{arg}'")
        i += 1
    return flags


def _init(args: List[str]) -> None:
    try:
        flags = _parse_flags(args)
    except ValueError as exc:
        emit_error(f"model_router: {exc}")
        emit_info(USAGE)
        return
    path = config_path()
    if path.exists() and not flags.get("force"):
        emit_warning(f"model_router: {path} exists; add --force to overwrite")
        return

    flagship = flags.get("flagship")
    if not flagship:
        from code_puppy.config import get_global_model_name

        flagship = get_global_model_name()
    if not flagship or flagship == "auto":
        emit_error(
            "model_router: pass --flagship <model>; the current model cannot be used as the flagship"
        )
        return

    cfg = template_config(flagship, cheap=flags.get("cheap"), mid=flags.get("mid"))
    known = ROUTER.known_models()
    unknown = [m for m in cfg.referenced_models() if known and m not in known]
    if unknown:
        emit_error(
            f"model_router: not registered models: {', '.join(unknown)}. Use names from /model."
        )
        return
    save_config(cfg, path)
    ROUTER.invalidate()
    emit_success(f"model_router: wrote {path}")
    emit_info(
        "Edit the tiers so cheap turns go to a cheap model, then run /model "
        f"{cfg.virtual_model} to switch routing on. Set {cfg.judge.api_key_env} for the judge API key."
    )


def _route(text: str) -> None:
    cfg = ROUTER.config()
    if cfg is None:
        emit_warning(f"model_router: {ROUTER.config_error}")
        return
    if not text.strip():
        emit_error("model_router: give a prompt to judge: /router route <prompt>")
        return
    decision = ROUTER.decide(text, [], cfg)
    _show_decision(decision)


def _show_decision(decision) -> None:
    if decision.reason == REASON_NO_API_KEY:
        cfg = ROUTER.config()
        env = (
            cfg.judge.api_key_env
            if cfg and cfg.judge.api_key_env
            else "the judge API key"
        )
        emit_warning(
            f"model_router: {env} is not set, so every turn goes to the fallback model. "
            f"Set it with /set {env.lower()}=<key>."
        )
    lines = [f"decision: {decision.summary()}"]
    for note in decision.notes:
        lines.append(f"  - {note}")
    if decision.probabilities:
        probs = ", ".join(
            f"L{k}={v:.2f}" for k, v in sorted(decision.probabilities.items())
        )
        lines.append(f"  probabilities: {probs}")
    if decision.judge_latency_ms is not None:
        lines.append(
            f"  judge: {decision.judge_model} in {decision.judge_latency_ms} ms"
        )
    if decision.error:
        lines.append(f"  error: {decision.error}")
    emit_info("\n".join(lines))


def _last() -> None:
    if ROUTER.last_decision is None:
        emit_info("model_router: no decision yet this session")
        return
    _show_decision(ROUTER.last_decision)


def _log(args: List[str]) -> None:
    limit = 20
    if args:
        try:
            limit = max(1, int(args[0]))
        except ValueError:
            emit_error(f"model_router: '{args[0]}' is not a number")
            return
    entries = ROUTER.read_log(limit)
    if not entries:
        emit_info(f"model_router: no routed runs logged yet ({log_path()})")
        return
    by_model = Counter(e.get("model") for e in entries)
    by_reason = Counter((e.get("decision") or {}).get("reason") for e in entries)
    lines = [f"last {len(entries)} routed runs:"]
    for entry in entries:
        decision = entry.get("decision") or {}
        level = decision.get("level")
        tokens = f"{entry.get('input_tokens') or 0}/{entry.get('output_tokens') or 0}"
        lines.append(
            f"  {entry.get('ts', '')}  {entry.get('model')}  {decision.get('reason')}"
            f"{'' if level is None else f' L{level}'}  tokens in/out {tokens}"
        )
    lines.append(
        "by model:  " + ", ".join(f"{m}={n}" for m, n in by_model.most_common())
    )
    lines.append(
        "by reason: " + ", ".join(f"{r}={n}" for r, n in by_reason.most_common())
    )
    emit_info("\n".join(lines))


def _toggle(enabled: bool) -> None:
    cfg = ROUTER.config(reload=True)
    if cfg is None:
        emit_warning(f"model_router: {ROUTER.config_error}")
        return
    cfg.enabled = enabled
    save_config(cfg)
    ROUTER.invalidate()
    emit_success(f"model_router: routing {'on' if enabled else 'off'}")


def _handle_custom_command(command: str, name: str) -> Optional[bool]:
    if name != "router":
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    args = parts[1:]
    sub = args[0] if args else "status"
    rest = args[1:]
    try:
        if sub == "status":
            _status()
        elif sub == "models":
            _models()
        elif sub == "init":
            _init(rest)
        elif sub == "route":
            _route(command.split(None, 2)[2] if len(command.split(None, 2)) > 2 else "")
        elif sub == "last":
            _last()
        elif sub == "log":
            _log(rest)
        elif sub == "on":
            _toggle(True)
        elif sub == "off":
            _toggle(False)
        elif sub == "reload":
            ROUTER.invalidate()
            cfg = ROUTER.config()
            if cfg is None:
                emit_warning(f"model_router: {ROUTER.config_error}")
            else:
                emit_success("model_router: config reloaded")
        elif sub in ("help", "-h", "--help"):
            emit_info(USAGE)
        else:
            emit_error(f"model_router: unknown subcommand '{sub}'")
            emit_info(USAGE)
    except RouterConfigError as exc:
        emit_error(f"model_router: {exc}")
    return True


# ---------------------------------------------------------------------------
# Hook registrations
# ---------------------------------------------------------------------------

register_callback("load_models_config", _load_models)
register_callback("register_model_type", _register_model_types)
register_callback("model_select", _model_select)
register_callback("agent_run_end", _agent_run_end)
register_callback("custom_command_help", _custom_help)
register_callback("custom_command", _handle_custom_command)
register_callback("register_completion_provider", _completion_provider)
