"""``EventBridge`` — translate Code Puppy runtime events into ACP updates.

While Code Puppy runs a turn, pydantic-ai's ``event_stream_handler`` fires the
``stream_event`` callback for each streamed text/thinking delta, and the tool
layer fires ``pre_tool_call`` / ``post_tool_call``. This bridge registers those
hooks (the same seam the shipping ``frontend_emitter`` plugin uses) and maps
each event to a ``session/update`` notification sent through the SDK's
``AgentSideConnection.session_update``.

We use hooks rather than consuming the ``MessageBus`` because the bus is
single-consumer and buffers until a renderer attaches, and agent text does not
arrive there as clean deltas. See ``README.md`` → *Event source*.

Tool-call titles and ``kind`` come from the shared banner contract
(``code_puppy.banners``), so ACP clients show the same labels as the terminal.
Each call also carries file ``locations`` and, on completion, the result text and
unified diff as inline content so edits are visible in the client's UI.
Plugin notices (``banner_notice`` phase) are forwarded as finished ``tool_call``
rows carrying ``_meta.code_puppy.banner``.

Routing/run-context (which session is active, whether text streamed, the
open-tool-call stack) lives in ``state`` so the permission and I/O backends can
share it. This bridge is pure translation.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

from acp.helpers import (
    start_tool_call,
    text_block,
    tool_content,
    update_agent_message_text,
    update_agent_thought_text,
    update_tool_call,
)
from acp.schema import ToolCallLocation

from code_puppy.banners import (
    Banner,
    banner_for_call,
    banner_for_result,
    result_text,
)
from code_puppy.callbacks import register_callback, unregister_callback
from code_puppy_core_plugins.acp import state
from code_puppy.tools.common import resolve_path

logger = logging.getLogger(__name__)

# Cap on inline diff/output content so a huge edit never floods the client.
_MAX_DIFF_CHARS = 8000

# Interactive TUI tools that can't run headless over ACP (they'd render a terminal
# picker + read stdin, the JSON-RPC pipe). Until ACP ratifies structured elicitation
# (an RFD), block these and steer the model to plain-text questions instead.
_INTERACTIVE_TOOLS = {"ask_user_question"}
_INTERACTIVE_BLOCK = {
    "blocked": True,
    "error_message": (
        "[BLOCKED] Interactive pickers aren't available over ACP. Ask the user "
        "your question(s) directly in your normal text response and wait for "
        "their reply — do not call this tool."
    ),
}


# Tool result for a question the client is presenting (see
# ``capabilities.client_presents_questions``).
_CLIENT_QUESTION_RESULT = (
    "The client is showing these questions to the user in its own question "
    "UI. End this turn and wait for the user's answers, which arrive in their "
    "next message. Do not repeat the questions in text or call this tool again."
)


class EventBridge:
    """Forward Code Puppy runtime hook events to the client as updates."""

    #: Set at ``initialize`` when the client opted in to presenting
    #: ``ask_user_question`` itself.
    question_cards_enabled = False

    def __init__(self) -> None:
        # Strong refs so scheduled notice sends are not garbage-collected mid-flight.
        self._notice_tasks: set[asyncio.Task] = set()

    def register(self) -> None:
        """Register the runtime hooks. Call once when the connection opens."""
        register_callback("stream_event", self._on_stream_event)
        register_callback("pre_tool_call", self._on_pre_tool_call)
        register_callback("post_tool_call", self._on_post_tool_call)
        register_callback("banner_notice", self._on_banner_notice)

    def unregister(self) -> None:
        """Remove the runtime hooks. Call on teardown so no callbacks leak.

        Mirrors ``register`` so a closed connection leaves no dangling bridge
        callbacks wired into the global registry (which would double-emit if a
        second connection opened in the same process).
        """
        for phase, handler in (
            ("stream_event", self._on_stream_event),
            ("pre_tool_call", self._on_pre_tool_call),
            ("post_tool_call", self._on_post_tool_call),
            ("banner_notice", self._on_banner_notice),
        ):
            try:
                unregister_callback(phase, handler)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "ACP: bridge unregister failed for %s", phase, exc_info=True
                )

    # ---- Hook handlers ----------------------------------------------------
    async def _on_stream_event(
        self, event_type: str, event_data: Any, agent_session_id: Optional[str] = None
    ) -> None:
        """Map streamed text/thinking to message/thought chunks.

        Two event shapes carry visible content and BOTH must be forwarded:

        * ``part_start`` -- a new text/thinking part whose *initial* content
          (``part.content``) is the opening of the message. pydantic-ai often
          packs the first token(s) here rather than in the first delta.
        * ``part_delta`` -- each subsequent chunk (``delta.content_delta``).

        Start-content and delta-content are disjoint (deltas are only the
        *additions* after the start), so emitting both reconstructs the full
        message with no overlap. Forwarding only ``part_delta`` -- as this
        bridge used to -- silently drops the opening chunk whenever the model
        front-loads it into the start event.

        The per-run ``state`` session id wins over ``agent_session_id``: the
        former is a ``ContextVar`` isolated to this prompt's task, whereas
        ``agent_session_id`` is derived from the process-wide message-bus
        session context, which a concurrent prompt on the same connection can
        overwrite. We only fall back to it when no run is active in this context.
        """
        session_id = state.get_active_session_id() or agent_session_id
        if session_id is None:
            return
        data = event_data if isinstance(event_data, dict) else {}
        if event_type == "part_start":
            part = data.get("part")
            content = getattr(part, "content", None)
            is_thinking = "Thinking" in (data.get("part_type", "") or "")
        elif event_type == "part_delta":
            delta = data.get("delta")
            content = (
                getattr(delta, "content_delta", None) if delta is not None else None
            )
            is_thinking = "Thinking" in (data.get("delta_type", "") or "")
        else:
            return
        if not content:
            return
        if is_thinking:
            update = update_agent_thought_text(content)
        else:
            state.note_streamed_text()
            update = update_agent_message_text(content)
        await self._send(session_id, update)

    async def _on_pre_tool_call(
        self, tool_name: str, tool_args: Dict[str, Any], context: Any = None
    ) -> Any:
        """Announce a tool call starting (status ``in_progress``).

        Interactive TUI tools that can't run over ACP are blocked here (via the
        ``pre_tool_call`` blocking contract) *before* we open a tool-call entry,
        so the client never sees a dangling in-progress call.
        """
        session_id = state.get_active_session_id()
        if session_id is None:
            return None
        if tool_name == "ask_user_question" and self.question_cards_enabled:
            return await self._present_question(session_id, tool_args)
        if tool_name in _INTERACTIVE_TOOLS:
            return dict(_INTERACTIVE_BLOCK)
        tool_call_id = state.push_tool_call(tool_name)
        banner = banner_for_call(tool_name, tool_args)
        update = start_tool_call(
            tool_call_id,
            banner.title,
            kind=banner.kind,
            status="in_progress",
            locations=self._locations_for(banner),
            raw_input=tool_args if isinstance(tool_args, dict) else {},
        )
        await self._send(session_id, self._with_banner(update, banner.to_wire()))

    async def _present_question(
        self, session_id: str, tool_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Hand ``ask_user_question`` to a client that presents it itself.

        The client gets a completed tool call whose ``rawInput`` carries the
        questions. The terminal picker is still blocked (stdin is the
        JSON-RPC pipe), and the model receives ``tool_result`` -- "wait for
        the answers" -- as the tool's return value instead of a policy deny.
        """
        await self._send(
            session_id,
            start_tool_call(
                uuid.uuid4().hex,
                "ask_user_question",
                kind="other",
                status="completed",
                raw_input=tool_args if isinstance(tool_args, dict) else {},
            ),
        )
        return {
            "blocked": True,
            "tool_result": _CLIENT_QUESTION_RESULT,
            # Shown instead on cores without ``tool_result`` support.
            "error_message": _CLIENT_QUESTION_RESULT,
        }

    async def _on_post_tool_call(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        result: Any,
        duration_ms: float,
        context: Any = None,
    ) -> None:
        """Report a tool call finishing (status ``completed`` / ``failed``)."""
        session_id = state.get_active_session_id()
        if session_id is None:
            return
        tool_call_id = state.pop_tool_call(tool_name)
        if tool_call_id is None:
            return
        banner = banner_for_result(tool_name, tool_args, result, duration_ms)
        content = []
        text = result_text(result)
        if text:
            content.append(tool_content(text_block(text)))
        content.extend(self._content_for(result))
        update = update_tool_call(
            tool_call_id,
            status=banner.status,
            title=banner.title,
            content=content or None,
            raw_output=self._summarize(result),
        )
        await self._send(session_id, self._with_banner(update, banner.to_wire()))

    def _on_banner_notice(self, banner: Dict[str, Any]) -> None:
        """Forward a plugin notice as a finished ``tool_call`` row (never reply text)."""
        # Sync hook: ``_trigger_callbacks_sync`` may run inside a live loop, so the
        # async send is scheduled, and the session id is read before scheduling.
        session_id = state.get_active_session_id()
        if session_id is None:
            return
        try:
            update = self._with_banner(
                start_tool_call(
                    f"notice-{uuid.uuid4().hex}",
                    banner["title"],
                    kind="other",
                    status="completed",
                ),
                banner,
            )
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not None:
                task = running.create_task(self._send(session_id, update))
                self._notice_tasks.add(task)
                task.add_done_callback(self._notice_tasks.discard)
                return
            # Worker threads have no running loop; hand the send to the ACP loop.
            loop = state.get_loop()
            if loop is not None and not loop.is_closed():
                asyncio.run_coroutine_threadsafe(self._send(session_id, update), loop)
        except Exception:  # noqa: BLE001
            logger.debug("ACP: dropping plugin notice", exc_info=True)

    # ---- Helpers ----------------------------------------------------------
    @staticmethod
    def _locations_for(banner: Banner) -> Optional[List[ToolCallLocation]]:
        """Return the banner's file as an ACP location, resolved against the session cwd.

        ``resolve_path`` (not ``os.path.abspath``) matches the tools' own base;
        the ACP process cwd would point the client at the wrong file.
        """
        if banner.target_type == "path" and banner.target:
            return [ToolCallLocation(path=resolve_path(banner.target))]
        return None

    @staticmethod
    def _with_banner(update: Any, banner: Dict[str, Any]) -> Any:
        """Attach the banner wire dict as ``_meta``; ACP helpers don't expose ``field_meta``."""
        return update.model_copy(
            update={"field_meta": {"code_puppy": {"banner": banner}}}
        )

    @staticmethod
    def _content_for(result: Any) -> list:
        """Attach the unified diff (if any) as an inline content block.

        File-edit tools return a ``diff`` (unified diff text). We surface it as
        a fenced ``diff`` code block so the change is visible inline in the
        client's tool-call entry, in every ACP client — independent of whether
        filesystem delegation is rendering a native diff too.
        """
        if not isinstance(result, dict):
            return []
        diff = result.get("diff")
        if not isinstance(diff, str) or not diff.strip():
            return []
        if len(diff) > _MAX_DIFF_CHARS:
            diff = diff[: _MAX_DIFF_CHARS - 3] + "..."
        return [tool_content(text_block(f"```diff\n{diff}\n```"))]

    async def _send(self, session_id: str, update: Any) -> None:
        """Emit one ``session/update`` notification, swallowing failures.

        A translation/transport hiccup on one event must never abort the agent
        run, so we log and move on.
        """
        connection = state.get_connection()
        if connection is None:
            return
        try:
            await connection.session_update(session_id, update)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to emit session/update")

    @staticmethod
    def _summarize(result: Any) -> Any:
        """Return a JSON-safe, size-bounded view of a tool result."""
        if result is None or isinstance(result, (str, int, float, bool, dict, list)):
            if isinstance(result, str) and len(result) > 4000:
                return result[:3997] + "..."
            return result
        rendered = str(result)
        return rendered[:3997] + "..." if len(rendered) > 4000 else rendered
