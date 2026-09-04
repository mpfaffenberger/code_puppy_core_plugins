"""Stable-id shim for Copilot's ``/responses`` SSE stream.

GitHub Copilot proxies the OpenAI Responses API but re-encrypts every
identifier on every event.  Observed on ``api.enterprise.githubcopilot.com``
for ``gpt-5.6-terra``: the ``id`` in ``response.output_item.added``, the
``item_id`` on each ``response.function_call_arguments.delta`` /
``response.output_text.delta``, and the ``item.id`` in
``response.output_item.done`` are all *different* opaque strings for the
same output item.

pydantic-ai's Responses stream handler keys parts by that id
(``vendor_part_id=chunk.item_id``), so with Copilot every delta lands on a
brand-new part: tool calls come through with empty ``arguments`` (the
``read_file ... Field required: file_path`` failure) and assistant text is
split into one part per delta.

This module wraps the httpx response byte-stream and rewrites each event so
every item keeps the first id seen for its ``output_index``.  Bytes that
are not SSE ``data:`` JSON events pass through untouched.  The wrapper
follows the ``_OpaqueCapturingStream`` pattern in ``reasoning_client``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

import httpx
from httpx import AsyncByteStream

logger = logging.getLogger(__name__)

_DATA_PREFIX = b"data:"
_DONE_MARKER = b"[DONE]"
_ITEM_ADDED = "response.output_item.added"
# Envelope events whose ``response.output`` lists complete items positionally.
_ENVELOPE_EVENTS = frozenset(
    {
        "response.completed",
        "response.incomplete",
        "response.failed",
        "response.done",
    }
)


def normalise_event_ids(event: Dict[str, Any], ids_by_index: Dict[int, str]) -> bool:
    """Rewrite *event* in place so each output item keeps one stable id.

    *ids_by_index* maps ``output_index`` -> the canonical id (first one seen,
    normally from ``response.output_item.added``).  Returns ``True`` when the
    event was modified.
    """
    if not isinstance(event, dict):
        return False

    changed = False
    event_type = event.get("type")

    # Envelope events: align ``response.output[i].id`` with what streamed.
    if event_type in _ENVELOPE_EVENTS:
        response = event.get("response")
        output = response.get("output") if isinstance(response, dict) else None
        if isinstance(output, list):
            for index, item in enumerate(output):
                if not isinstance(item, dict):
                    continue
                canonical = ids_by_index.get(index)
                if canonical is None:
                    if isinstance(item.get("id"), str):
                        ids_by_index[index] = item["id"]
                    continue
                if item.get("id") != canonical:
                    item["id"] = canonical
                    changed = True
        return changed

    output_index = event.get("output_index")
    if not isinstance(output_index, int):
        return False

    item = event.get("item")
    item = item if isinstance(item, dict) else None

    canonical = ids_by_index.get(output_index)
    if canonical is None:
        # First sighting of this output item -- remember whichever id it carries.
        candidate = item.get("id") if item else event.get("item_id")
        if isinstance(candidate, str) and candidate:
            ids_by_index[output_index] = candidate
            if event_type != _ITEM_ADDED:
                logger.debug(
                    "Copilot /responses: adopting id from %s for output_index %d",
                    event_type,
                    output_index,
                )
        return False

    if "item_id" in event and event["item_id"] != canonical:
        event["item_id"] = canonical
        changed = True
    if item is not None and "id" in item and item["id"] != canonical:
        item["id"] = canonical
        changed = True
    return changed


class _StableIdStream(AsyncByteStream):
    """Async byte-stream wrapper rewriting ids in Copilot Responses SSE.

    Buffers bytes, splits on newlines, rewrites ``data:`` JSON events via
    :func:`normalise_event_ids`, and re-emits them.  Everything else (event
    lines, blank separators, ``[DONE]``, unparseable payloads) is forwarded
    verbatim.  Inherits ``httpx.AsyncByteStream`` so ``Response.aclose()``
    treats it as an async stream.
    """

    def __init__(self, inner_stream: Any):
        self._inner = inner_stream
        self._buffer = b""
        self._ids_by_index: Dict[int, str] = {}

    def __aiter__(self):
        return self._iter_impl()

    async def _iter_impl(self):
        async for chunk in self._inner:
            out = self._feed(chunk)
            if out:
                yield out
        if self._buffer:
            tail, self._buffer = self._buffer, b""
            yield self._rewrite_line(tail)

    async def aclose(self):
        if hasattr(self._inner, "aclose"):
            await self._inner.aclose()

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    # -- SSE rewriting -----------------------------------------------------

    def _feed(self, chunk: bytes) -> bytes:
        self._buffer += chunk
        pieces: list[bytes] = []
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            pieces.append(self._rewrite_line(line))
            pieces.append(b"\n")
        return b"".join(pieces)

    def _rewrite_line(self, line: bytes) -> bytes:
        if not line.startswith(_DATA_PREFIX):
            return line
        payload = line[len(_DATA_PREFIX) :].strip()
        if not payload or payload == _DONE_MARKER:
            return line
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return line
        if not normalise_event_ids(event, self._ids_by_index):
            return line
        return b"data: " + json.dumps(event, ensure_ascii=False).encode("utf-8")


def _is_responses_sse(request: httpx.Request, response: httpx.Response) -> bool:
    if request.method != "POST" or not request.url.path.rstrip("/").endswith(
        "/responses"
    ):
        return False
    content_type = response.headers.get("content-type", "")
    return "text/event-stream" in content_type.lower()


def patch_client_for_stable_ids(client: httpx.AsyncClient) -> None:
    """Monkey-patch *client* so ``/responses`` SSE streams carry stable ids.

    Call after creating the client and before handing it to
    ``OpenAIProvider``.  Only streaming ``/responses`` bodies are touched;
    everything else (``/chat/completions``, non-streaming JSON, errors)
    passes through the original ``send`` unchanged.
    """
    original_send = client.send

    async def _patched_send(
        request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        response = await original_send(request, *args, **kwargs)
        if (
            response.status_code == 200
            and not response.is_stream_consumed
            and _is_responses_sse(request, response)
            and getattr(response, "stream", None) is not None
        ):
            response.stream = _StableIdStream(response.stream)
        return response

    client.send = _patched_send  # type: ignore[method-assign]
