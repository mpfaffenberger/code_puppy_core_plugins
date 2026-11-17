"""Run context for the active ACP connection.

Code Puppy's approval and I/O seams live at its *edges* (the ``tools.common`` approval
backend, the legacy ``run_shell_command`` hook on older cores, and the
``tools.io_backends`` filesystem/command backends). Those are plain module
functions with no reference to the live ACP connection, so this module is the
small, explicit bridge between them and the running agent.

Two kinds of state live here:

* **Connection-scoped** — the ``AgentSideConnection`` handle + its event loop.
  There is exactly one connection per process (set once, on connect), so these
  are plain module globals.
* **Run-scoped** — the id of the session whose prompt is currently running, the
  stack of in-flight tool calls (so permission dialogs and updates correlate to
  the tool that triggered them), and whether any assistant text has streamed.
  A single ACP connection multiplexes *multiple* sessions, and the SDK
  dispatches each ``session/prompt`` as its own task, so two prompts can be in
  flight at once. Run-scoped state therefore lives in a **``ContextVar``** — set
  in ``begin_run`` before the run task is spawned, so each prompt task (and the
  tool coroutines/threads it copies its context into) sees its *own* run, never
  a sibling's. The var holds a shared mutable dict so a mutation made deep in
  the run (e.g. ``note_streamed_text`` from a streamed delta) is visible back in
  the prompt coroutine's ``finally`` that reads it.

Keeping this here — rather than smuggled through globals or reached via the
agent object — keeps the coupling visible and testable.
"""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from typing import Any, Dict, Optional, Tuple

_CONNECTION: Any = None
_LOOP: Optional[asyncio.AbstractEventLoop] = None

# Per-run state isolated per prompt task via a ContextVar (None = no active run).
# A mutable dict {"session_id", "streamed", "tool_stack"} so child tasks/threads
# (which copy this context) see writes made by the creating prompt coroutine.
_RUN: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "acp_run", default=None
)


# ---- Connection lifecycle -------------------------------------------------
def set_connection(connection: Any, loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Register (or clear, with ``None``) the live connection + its loop."""
    global _CONNECTION, _LOOP
    _CONNECTION = connection
    _LOOP = loop


def get_connection() -> Any:
    """Return the ``AgentSideConnection``, or ``None`` when not in ACP mode."""
    return _CONNECTION


def get_loop() -> Optional[asyncio.AbstractEventLoop]:
    """Return the ACP event loop, or ``None`` when not in ACP mode."""
    return _LOOP


# ---- Per-run context ------------------------------------------------------
def begin_run(session_id: str) -> None:
    """Mark ``session_id`` as the run whose events/permissions we handle.

    Sets a fresh run-state dict in the current context. Call this in the prompt
    coroutine *before* spawning the run task, so the task (and every tool
    coroutine/thread that copies the context) shares this exact dict.
    """
    _RUN.set(
        {
            "session_id": session_id,
            "streamed": False,
            "tool_stack": [],
        }
    )


def end_run() -> None:
    """Detach after a run; late events/permissions in this context are dropped."""
    _RUN.set(None)


def get_active_session_id() -> Optional[str]:
    """The session id of the in-flight run in this context, or ``None``."""
    run = _RUN.get()
    return run["session_id"] if run else None


# ---- Streamed-text tracking (final-result fallback) -----------------------
def note_streamed_text() -> None:
    """Record that at least one assistant text delta streamed this run."""
    run = _RUN.get()
    if run is not None:
        run["streamed"] = True


def streamed_text() -> bool:
    """Whether any assistant text streamed during the active run."""
    run = _RUN.get()
    return bool(run and run["streamed"])


# ---- Tool-call correlation ------------------------------------------------
def push_tool_call(
    tool_name: str, args_ref: object | None = None, *, runtime_id: str | None = None
) -> str:
    """Open a card correlated by runtime ID, or argument identity on old cores."""
    tool_call_id = f"tc_{uuid.uuid4().hex[:12]}"
    run = _RUN.get()
    if run is not None:
        run["tool_stack"].append((tool_name, tool_call_id, args_ref, runtime_id))
    return tool_call_id


def pop_tool_call(
    tool_name: str, args_ref: object | None = None, *, runtime_id: str | None = None
) -> Optional[str]:
    """Close only the matching identity; never fall back from an unknown ID."""
    run = _RUN.get()
    if run is None:
        return None
    stack = run["tool_stack"]
    for i in range(len(stack) - 1, -1, -1):
        name, tool_call_id, stored_args, stored_id = stack[i]
        matches = (
            stored_id == runtime_id
            if runtime_id is not None
            else stored_id is None and stored_args is args_ref
        )
        if name == tool_name and matches:
            stack.pop(i)
            return tool_call_id
    return None


def current_tool_call() -> Optional[Tuple[str, str]]:
    """Return the most recent open ``(tool_name, tool_call_id)``, or ``None``."""
    run = _RUN.get()
    if not run or not run["tool_stack"]:
        return None
    tool_name, tool_call_id, _args_ref, _runtime_id = run["tool_stack"][-1]
    return tool_name, tool_call_id
