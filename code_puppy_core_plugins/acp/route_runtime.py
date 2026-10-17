"""Build an ACP session's agent on one exact route.

A session's agent and model are session-local: building one never writes the
terminal's global model or agent, and never quietly substitutes another
model or agent. If the requested route cannot be built, the build fails and
the session keeps whatever route it had.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Tuple

from code_puppy_core_plugins.acp import mcp_config
from code_puppy_core_plugins.acp.route import SessionRoute


def _configured_models() -> list[str]:
    from code_puppy.command_line.model_picker_completion import load_model_names

    return list(load_model_names() or [])


def validate_startup_route(agent_name: str, model_id: Optional[str]) -> None:
    """Fail before serving when ``--agent`` / ``--model`` name nothing real."""
    from code_puppy.agents.agent_manager import get_available_agents

    if agent_name not in get_available_agents():
        raise ValueError(f"Agent '{agent_name}' not found")
    if model_id is not None and model_id not in _configured_models():
        raise ValueError(f"Model '{model_id}' is not configured")


def _load_exact_agent(agent_name: str) -> Any:
    from code_puppy.agents.agent_manager import get_available_agents, load_agent

    try:
        return load_agent(agent_name, allow_fallback=False)
    except TypeError:  # pragma: no cover - code_puppy without allow_fallback
        if agent_name not in get_available_agents():
            raise ValueError(f"Agent '{agent_name}' not found") from None
        return load_agent(agent_name)


def _pin_model(agent: Any, model_id: str) -> None:
    try:
        agent.set_runtime_model_name_override(model_id, allow_fallback=False)
    except TypeError:  # pragma: no cover - code_puppy without allow_fallback
        # The post-build check in ``build_agent`` still catches a fallback.
        agent.set_runtime_model_name_override(model_id)


def build_agent(
    agent_name: str,
    model_id: Optional[str],
    *,
    epoch: int,
    history: Optional[Iterable[Any]] = None,
    mcp_specs: Optional[list[Any]] = None,
) -> Tuple[Any, SessionRoute]:
    """Build ``agent_name`` on ``model_id`` (or its own default model) exactly.

    The pydantic-ai agent is built here, not lazily at the first prompt, so
    the returned route is the one actually built. Raises ``ValueError`` when
    the agent or model is unknown or the build would land on another model.
    """
    agent = _load_exact_agent(agent_name)
    effective_model = model_id or agent.get_model_name()
    if not effective_model:
        raise ValueError(f"No model is available for agent '{agent_name}'")
    if effective_model not in _configured_models():
        raise ValueError(f"Model '{effective_model}' is not configured")

    _pin_model(agent, effective_model)
    if history is not None:
        agent.set_message_history(list(history))
    if mcp_specs:
        mcp_config.attach(agent, list(mcp_specs))
    agent.reload_code_generation_agent()
    built_model = getattr(agent, "_last_model_name", effective_model)
    if built_model != effective_model:
        raise ValueError(
            f"Agent built model '{built_model}', expected exactly '{effective_model}'"
        )
    return agent, SessionRoute(agent_name, effective_model, epoch)
