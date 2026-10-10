"""``CodePuppyAgent`` — Code Puppy as a native Agent Client Protocol agent.

This implements the official SDK's ``Agent`` interface, so Code Puppy runs as
an external agent in any ACP client (Zed, and other editors that speak ACP).
The SDK owns the wire: ``acp.run_agent`` binds stdio, frames JSON-RPC, parses
params into typed models, and hands us a ``Client`` connection (via
``on_connect``) for talking back to the client. We own the behaviour: mapping ACP sessions to Code Puppy agents,
running prompts, and translating events (through ``EventBridge``) and I/O
(through ``permissions`` + ``io_delegation``).

Only capability-gated methods we actually support are implemented; the SDK's
router resolves unimplemented ones to a clean "method not found", so we never
have to stub protocol surface we don't mean.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Dict, List, Optional

from acp import Agent
from acp.schema import (
    AgentCapabilities,
    AuthenticateResponse,
    AvailableCommand,
    AvailableCommandsUpdate,
    ClientCapabilities,
    CloseSessionResponse,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    SessionInfo,
    SetSessionModeResponse,
)

from code_puppy_core_plugins.acp import (
    capabilities,
    io_delegation,
    permissions,
    persistence,
    replay,
    route_changes,
    route_runtime,
    session_config,
    state,
)
from code_puppy_core_plugins.acp.bridge import EventBridge
from code_puppy_core_plugins.acp.route import SessionRoute, capability_metadata
from code_puppy_core_plugins.acp.session import ACPSession

logger = logging.getLogger(__name__)

# ACP protocol version this plugin targets. Sourced from the SDK (not a literal) so a
# library upgrade can't silently advertise a stale version; falls back to v1 if absent.
try:
    from acp import PROTOCOL_VERSION as PROTOCOL_VERSION
except ImportError:  # pragma: no cover - defensive for older SDKs
    PROTOCOL_VERSION = 1


def _code_puppy_version() -> str:
    try:
        return version("code-puppy")
    except PackageNotFoundError:
        return "0.0.0"


class CodePuppyAgent(Agent):
    """One ACP connection's worth of Code Puppy, spread across client threads."""

    def __init__(
        self,
        *,
        default_agent_name: Optional[str] = None,
        default_model_id: Optional[str] = None,
        persistence_base_dir: Optional[Path] = None,
    ) -> None:
        """Set the route new sessions start on.

        ``default_agent_name`` / ``default_model_id`` come from ``--agent`` /
        ``--model`` and are checked up front, so a bad flag fails before the
        connection is served. Without them a session starts on the current
        agent and that agent's own model.
        """
        if default_agent_name is not None or default_model_id is not None:
            from code_puppy.agents.agent_manager import get_current_agent_name

            route_runtime.validate_startup_route(
                default_agent_name or get_current_agent_name(), default_model_id
            )
        self._sessions: Dict[str, ACPSession] = {}
        self._bridge = EventBridge()
        self._client_caps: Optional[ClientCapabilities] = None
        self._default_agent_name = default_agent_name
        self._default_model_id = default_model_id
        self._persistence_base_dir = persistence_base_dir

    # ---- Connection lifecycle ---------------------------------------------
    def on_connect(self, conn: Any) -> None:
        """Store the client handle + loop, wire events, install approvals.

        Called synchronously by the SDK from inside ``run_agent`` — already on
        the running event loop — so ``get_running_loop`` returns the loop our
        permission and I/O bridges marshal onto.
        """
        state.set_connection(conn, asyncio.get_running_loop())
        self._bridge.register()
        permissions.install()

    def shutdown(self) -> None:
        """Unwire this agent's event hooks. Call once the connection ends.

        ``permissions`` and ``io_delegation`` are torn down by the plugin entry
        point; this drops the ``EventBridge`` hooks it owns so a closed
        connection leaves the global callback registry clean.
        """
        self._bridge.unregister()

    # ---- Handshake --------------------------------------------------------
    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Optional[ClientCapabilities] = None,
        client_info: Optional[Implementation] = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        """Negotiate capabilities and install matching I/O delegation.

        Once we know what the client can do, reroute Code Puppy's workspace
        file I/O and shell to the client's ``fs/*`` / ``terminal/*`` methods
        (only the edges
        the client advertises). Everything else stays local.
        """
        self._client_caps = client_capabilities
        self._bridge.question_cards_enabled = capabilities.client_presents_questions(
            client_capabilities
        )
        io_delegation.install(client_capabilities)
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=self._agent_capabilities(),
            agent_info=Implementation(
                name="code-puppy",
                title="Code Puppy",
                version=_code_puppy_version(),
                field_meta=capability_metadata(),
            ),
        )

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse:
        """No auth flow — Code Puppy authenticates via its own model config."""
        return AuthenticateResponse()

    # ---- Session lifecycle ------------------------------------------------
    async def new_session(
        self,
        cwd: str,
        additional_directories: Optional[List[str]] = None,
        mcp_servers: Optional[List[Any]] = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        """Create a session bound to a fresh agent instance.

        Uses ``load_agent`` (not the cached ``get_current_agent``) so each
        client thread gets its own ``_message_history``. ``cwd`` +
        ``additional_directories`` anchor the session's tools; ``mcp_servers``
        the client injects are attached to the agent.
        """
        session_id = f"sess_{uuid.uuid4().hex[:16]}"
        session = self._make_session(
            session_id, cwd, additional_directories, mcp_servers
        )
        self._announce_commands_soon(session_id)
        return NewSessionResponse(
            session_id=session_id,
            config_options=session_config.config_options(session.route),
            field_meta=session.route.metadata(session_id),
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: Optional[List[str]] = None,
        mcp_servers: Optional[List[Any]] = None,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        """Re-open a thread the client remembers, replaying persisted history.

        If we have the session's history on disk (persisted after each turn),
        it is rehydrated into the fresh agent so the model continues the real
        conversation, AND replayed to the client as ``session/update``
        notifications so the client rebuilds the thread UI (without this the
        client shows -- and may discard -- an empty thread). Otherwise the
        thread is re-created empty but functional. A session that is still
        live in this process is reused as is, on its current route.
        """
        session = self._sessions.get(session_id) or self._make_session(
            session_id, cwd, additional_directories, mcp_servers, rehydrate=True
        )
        async with session.route_lock:
            if session.closing:
                raise ValueError(f"session is closing: {session_id}")
            await replay.replay_history(session_id, session.agent.get_message_history())
        self._announce_commands_soon(session_id)
        return LoadSessionResponse(
            config_options=session_config.config_options(session.route),
            field_meta=session.route.metadata(session_id),
        )

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: Optional[List[str]] = None,
        mcp_servers: Optional[List[Any]] = None,
        **kwargs: Any,
    ) -> Any:
        """Resume a session across a restart, rehydrating + replaying history."""
        from acp.schema import ResumeSessionResponse

        session = self._sessions.get(session_id) or self._make_session(
            session_id, cwd, additional_directories, mcp_servers, rehydrate=True
        )
        await replay.replay_history(session_id, session.agent.get_message_history())
        self._announce_commands_soon(session_id)
        return ResumeSessionResponse(
            config_options=session_config.config_options(session.route),
            field_meta=session.route.metadata(session_id),
        )

    async def fork_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: Optional[List[str]] = None,
        mcp_servers: Optional[List[Any]] = None,
        **kwargs: Any,
    ) -> Any:
        """Branch a session: duplicate its history into a new session id.

        The source's history is copied into a fresh agent so the fork continues
        the conversation independently. The source may be a live in-memory
        session OR one persisted by a prior process -- forking must survive a
        restart just like ``load``/``resume`` do, so we fall back to the
        pickled history when the source isn't live.

        The fork starts on the agent and model its source was on, as a new
        session at route epoch 1.
        """
        from acp.schema import ForkSessionResponse

        source = self._sessions.get(session_id)
        source_route: Optional[SessionRoute] = None
        if source is not None:
            async with source.route_lock:
                if source.closing:
                    raise ValueError(f"session is closing: {session_id}")
                source_history = list(source.agent.get_message_history())
                source_cwd = source.cwd
                source_route = source.route
        else:
            record, source_history = persistence.load_for_restore(
                session_id, self._persistence_base_dir
            )
            if record is None and not source_history:
                raise ValueError(f"unknown session to fork: {session_id}")
            source_cwd = record.cwd if record is not None else None
            source_route = record.route if record is not None else None
        new_id = f"sess_{uuid.uuid4().hex[:16]}"
        session = self._make_session(
            new_id,
            cwd or source_cwd or "",
            additional_directories,
            mcp_servers,
            route=(
                SessionRoute(source_route.agent_name, source_route.model_id, 1)
                if source_route is not None
                else None
            ),
            history=source_history,
        )
        self._announce_commands_soon(new_id)
        return ForkSessionResponse(
            session_id=new_id,
            config_options=session_config.config_options(session.route),
            field_meta=session.route.metadata(new_id),
        )

    async def set_session_mode(
        self, mode_id: str, session_id: str, **kwargs: Any
    ) -> SetSessionModeResponse:
        """No-op mode handler.

        Code Puppy has no ACP *session modes* (e.g. plan vs default); model
        selection is exposed as a ``model`` config option instead (that is what
        clients bind their model picker to -- see ``session_config``). This
        method exists only to satisfy the SDK router's ``session/set_mode``
        route; any mode id is accepted as a no-op.
        """
        return SetSessionModeResponse()

    async def set_config_option(
        self, config_id: str, session_id: str, value: Any, **kwargs: Any
    ) -> Any:
        """Apply a config-option change and return the refreshed options.

        A change to the ``model`` or ``agent`` option rebuilds this session's
        agent on the new route (history + client MCP servers preserved), so the
        client's pickers switch mid-thread. The change is session-local: it
        never writes the terminal's global model or agent. An unknown value is
        rejected and the session keeps its current route.
        """
        from acp.schema import SetSessionConfigOptionResponse

        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"unknown session: {session_id}")
        if config_id in (
            session_config.AGENT_OPTION_ID,
            session_config.MODEL_OPTION_ID,
        ):
            await route_changes.change_route(session, config_id, str(value), kwargs)
        else:
            session_config.apply_config_option(config_id, value, session.route)
        return SetSessionConfigOptionResponse(
            config_options=session_config.config_options(session.route),
            field_meta=session.route.metadata(session_id),
        )

    async def list_sessions(
        self, cursor: Optional[str] = None, cwd: Optional[str] = None, **kwargs: Any
    ) -> ListSessionsResponse:
        """List every revivable session: live in-memory + persisted on disk.

        Live sessions (this process) merge with sessions persisted to disk in
        prior runs, so a client's picker still shows -- and can
        ``session/load`` / ``session/resume`` -- threads that outlived the
        process that made them. Live wins on id collision (it carries the
        freshest state). Uncursored single page; optional ``cwd`` filter.
        """
        infos: List[SessionInfo] = []
        seen: set[str] = set()
        for s in self._sessions.values():
            if cwd is not None and s.cwd != cwd:
                continue
            seen.add(s.session_id)
            infos.append(
                SessionInfo(
                    session_id=s.session_id,
                    cwd=s.cwd or cwd or "",
                    additional_directories=s.additional_directories or None,
                )
            )
        for record in persistence.list_persisted(**self._persistence_kwargs()):
            if record.session_id in seen:
                continue
            if cwd is not None and record.cwd != cwd:
                continue
            seen.add(record.session_id)
            infos.append(
                SessionInfo(
                    session_id=record.session_id,
                    cwd=record.cwd or cwd or "",
                    additional_directories=record.additional_directories or None,
                )
            )
        return ListSessionsResponse(sessions=infos, next_cursor=None)

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> CloseSessionResponse:
        """Drop a session, cancelling any in-flight run first.

        A close is a deliberate act, so its persisted history is deleted too --
        otherwise the session would resurrect in the next ``list_sessions``,
        which is exactly the "disappeared session that won't stay gone" bug in
        reverse.

        Also purges this session's own bucket of
        ``load_model_with_fallback``'s once-per-conversation dead-model-warning
        dedup (keyed by the session's conversation root id -- see
        ``subagent_invocation.py`` / ``subagent_context.py``). Sub-agent
        warnings are scoped per session precisely so they never leak into any
        OTHER session; without this, a long-running server churning through
        many short-lived sessions would accumulate one dangling bucket per
        closed session forever.

        Waits for any in-flight run to actually finish unwinding
        (``cancel_and_wait``, not the bare ``cancel``) before purging --
        otherwise a run still mid-flight inside ``_invoke_agent_impl`` could
        write a fresh warning into this session's bucket a moment *after*
        the purge, leaking that one entry forever (nothing will call
        ``close_session`` for this id a second time).

        The session is marked closing first and the delete happens under its
        route lock, so a route change or turn still saving cannot write the
        session back to disk after it was deleted.
        """
        from code_puppy.agents._builder import reset_model_fallback_warnings

        session = self._sessions.get(session_id)
        if session is not None:
            session.closing = True
            session.cancel()
            async with session.route_lock:
                await session.cancel_and_wait()
                if self._sessions.get(session_id) is session:
                    self._sessions.pop(session_id)
                persistence.delete(session_id, **self._persistence_kwargs())
        else:
            persistence.delete(session_id, **self._persistence_kwargs())
        reset_model_fallback_warnings(scope=session_id)
        return CloseSessionResponse()

    # ---- Prompt turn ------------------------------------------------------
    async def prompt(
        self, prompt: List[Any], session_id: str, **kwargs: Any
    ) -> PromptResponse:
        """Run the agent on a user prompt, streaming updates via the bridge."""
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"unknown session: {session_id}")
        route_change = route_changes.route_command(prompt)
        if route_change is not None:
            config_id, value = route_change
            await self.set_config_option(config_id, session_id, value, **kwargs)
            await route_changes.emit_switch_confirmation(session, config_id)
            return PromptResponse(stop_reason="end_turn")
        outcome = await session.prompt(prompt)
        return PromptResponse(stop_reason=outcome.stop_reason, usage=outcome.usage)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        """Cancel the in-flight run for a session (notification)."""
        session = self._sessions.get(session_id)
        if session is not None:
            session.cancel()

    # ---- Helpers ----------------------------------------------------------
    def _agent_capabilities(self) -> AgentCapabilities:
        return capabilities.agent_capabilities()

    def _make_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: Optional[List[str]],
        mcp_servers: Optional[List[Any]],
        *,
        rehydrate: bool = False,
        route: Optional[SessionRoute] = None,
        history: Optional[List[Any]] = None,
    ) -> ACPSession:
        """Build + register an ``ACPSession`` with a fresh agent.

        When ``rehydrate`` is set, any persisted history for ``session_id`` is
        replayed into the agent so the model continues the real conversation.
        Client-injected ``mcp_servers`` are attached best-effort.

        Also resets the SHARED/unscoped bucket of
        ``load_model_with_fallback``'s once-per-conversation
        pinned-model-unavailable dedup (see ``_builder.reset_model_fallback_warnings``).
        Sub-agent invocations scope their own warning state to the
        conversation's ROOT id (a ContextVar -- see
        ``subagent_context.get_conversation_root_id``/``session.py``'s prompt
        handler), so they already can't leak across sessions and don't need
        this reset at all. The *main* agent build path
        (``build_pydantic_agent``, used to build each session's own
        top-level agent) still shares one unscoped bucket across every
        session in this process, matching the pre-existing
        single-main-agent-per-process model -- so without this reset, session
        A hitting a dead pin during its own build could silently suppress the
        identical warning for session B's build too. Resetting only the
        ``scope=None`` bucket on every new/loaded/forked session avoids that
        without touching any other session's already-scoped sub-agent
        warnings.
        """
        from code_puppy.agents._builder import reset_model_fallback_warnings

        if session_id in self._sessions:
            raise ValueError(f"session is already live: {session_id}")
        reset_model_fallback_warnings(scope=None)
        if rehydrate:
            record, history = persistence.load_for_restore(
                session_id, self._persistence_base_dir
            )
            route = record.route if record is not None else None
        agent_name, model_id, epoch = self._route_parts(route)
        agent, effective_route = route_runtime.build_agent(
            agent_name,
            model_id,
            epoch=epoch,
            history=history,
            mcp_specs=mcp_servers,
        )
        session = ACPSession(
            session_id,
            agent,
            cwd=cwd,
            additional_directories=additional_directories,
            mcp_specs=mcp_servers,
            route=effective_route,
            persistence_base_dir=self._persistence_base_dir,
        )
        self._sessions[session_id] = session
        return session

    def _route_parts(
        self, route: Optional[SessionRoute]
    ) -> tuple[str, Optional[str], int]:
        """``(agent, model, epoch)`` to build: ``route``, or the default route."""
        if route is not None:
            return route.agent_name, route.model_id, route.epoch
        agent_name = self._default_agent_name
        if agent_name is None:
            from code_puppy.agents.agent_manager import get_current_agent_name

            agent_name = get_current_agent_name()
        return agent_name, self._default_model_id, 1

    def _persistence_kwargs(self) -> Dict[str, Any]:
        if self._persistence_base_dir is None:
            return {}
        return {"base_dir": self._persistence_base_dir}

    def _announce_commands_soon(self, session_id: str) -> None:
        """Schedule an ``available_commands_update`` after the response ships.

        We must not send the update before the ``new_session``/``load_session``
        reply reaches the client (it wouldn't know the session yet).
        Scheduling a
        task defers it until this coroutine yields, i.e. after the reply.
        """
        loop = state.get_loop()
        if loop is not None:
            loop.create_task(self._announce_commands(session_id))

    async def _announce_commands(self, session_id: str) -> None:
        """Tell the client which slash commands Code Puppy exposes.

        Deferred behind a short delay: ``create_task`` only yields one tick,
        which isn't enough to guarantee the ``new_session`` reply has been
        serialized onto the wire. Without this the client can receive the
        notification before it knows the session id and drop it as "unknown
        session". 100ms is imperceptible and comfortably after the reply.
        """
        await asyncio.sleep(0.1)
        connection = state.get_connection()
        if connection is None:
            return
        try:
            from code_puppy.command_line.command_registry import get_unique_commands

            available = [
                AvailableCommand(name=c.name, description=c.description)
                for c in get_unique_commands()
            ]
        except Exception:  # noqa: BLE001
            logger.debug("ACP: could not enumerate slash commands", exc_info=True)
            return
        if not available:
            return
        update = AvailableCommandsUpdate(
            session_update="available_commands_update",
            available_commands=available,
        )
        try:
            await connection.session_update(session_id, update)
        except Exception:  # noqa: BLE001
            logger.debug("ACP: available_commands_update failed", exc_info=True)
