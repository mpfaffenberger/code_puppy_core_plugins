"""Tests for the Copilot ``/responses`` stable-id SSE shim.

Copilot re-encrypts ``id`` / ``item_id`` on every streamed event; pydantic-ai
keys streamed parts by that id, so tool-call arguments and text fragments
would otherwise scatter across parts.
"""

from __future__ import annotations

import json

import httpx
import pytest

from code_puppy_core_plugins.copilot_auth.responses_stream import (
    _StableIdStream,
    normalise_event_ids,
    patch_client_for_stable_ids,
)


def _sse(event: dict) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


class _FakeInner(httpx.AsyncByteStream):
    """Async byte stream over pre-cut chunks (httpx reads bytes content eagerly,
    so mock responses must carry a real stream to exercise the shim)."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for c in self._chunks:
            yield c

    async def aclose(self):
        self.closed = True


def _parse_events(raw: bytes) -> list[dict]:
    out = []
    for line in raw.decode().split("\n"):
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            out.append(json.loads(line[5:]))
    return out


# Shaped like the real gpt-5.6-terra stream: same item, four different ids.
TOOL_CALL_EVENTS = [
    {
        "type": "response.output_item.added",
        "output_index": 1,
        "sequence_number": 4,
        "item": {
            "type": "function_call",
            "id": "id-added",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": "",
            "status": "in_progress",
        },
    },
    {
        "type": "response.function_call_arguments.delta",
        "output_index": 1,
        "item_id": "id-delta-1",
        "delta": '{"file_path":',
    },
    {
        "type": "response.function_call_arguments.delta",
        "output_index": 1,
        "item_id": "id-delta-2",
        "delta": '"notes.txt"}',
    },
    {
        "type": "response.function_call_arguments.done",
        "output_index": 1,
        "item_id": "id-done-args",
        "arguments": '{"file_path":"notes.txt"}',
    },
    {
        "type": "response.output_item.done",
        "output_index": 1,
        "item": {
            "type": "function_call",
            "id": "id-item-done",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": '{"file_path":"notes.txt"}',
            "status": "completed",
        },
    },
]


class TestNormaliseEventIds:
    def test_all_ids_follow_the_added_event(self):
        ids: dict[int, str] = {}
        events = [json.loads(json.dumps(e)) for e in TOOL_CALL_EVENTS]
        changed = [normalise_event_ids(e, ids) for e in events]

        assert changed == [False, True, True, True, True]
        assert ids == {1: "id-added"}
        assert events[1]["item_id"] == "id-added"
        assert events[2]["item_id"] == "id-added"
        assert events[3]["item_id"] == "id-added"
        assert events[4]["item"]["id"] == "id-added"
        # Non-id payload untouched.
        assert events[4]["item"]["arguments"] == '{"file_path":"notes.txt"}'
        assert events[4]["item"]["call_id"] == "call_1"

    def test_independent_output_indexes_keep_their_own_ids(self):
        ids: dict[int, str] = {}
        normalise_event_ids(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": "r0"},
            },
            ids,
        )
        normalise_event_ids(
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {"id": "f1"},
            },
            ids,
        )
        ev = {"type": "response.output_text.delta", "output_index": 0, "item_id": "zzz"}
        assert normalise_event_ids(ev, ids)
        assert ev["item_id"] == "r0"
        assert ids == {0: "r0", 1: "f1"}

    def test_adopts_first_seen_id_when_added_event_missing(self):
        ids: dict[int, str] = {}
        first = {
            "type": "response.output_text.delta",
            "output_index": 0,
            "item_id": "a",
        }
        second = {
            "type": "response.output_text.delta",
            "output_index": 0,
            "item_id": "b",
        }
        assert not normalise_event_ids(first, ids)
        assert normalise_event_ids(second, ids)
        assert second["item_id"] == "a"

    def test_already_consistent_stream_is_left_alone(self):
        ids: dict[int, str] = {}
        added = {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "x"},
        }
        delta = {
            "type": "response.output_text.delta",
            "output_index": 0,
            "item_id": "x",
        }
        assert not normalise_event_ids(added, ids)
        assert not normalise_event_ids(delta, ids)

    @pytest.mark.parametrize(
        "event",
        [
            {"type": "response.created", "response": {"id": "abc"}},
            {"type": "response.in_progress", "response": {"id": "def"}},
            {"type": "response.output_text.delta", "item_id": "x"},  # no output_index
            {"type": "response.output_text.delta", "output_index": "1", "item_id": "x"},
            {"type": "weird"},
            "not a dict",
            None,
        ],
    )
    def test_events_without_output_index_are_untouched(self, event):
        ids: dict[int, str] = {}
        snapshot = json.dumps(event)
        assert normalise_event_ids(event, ids) is False
        assert json.dumps(event) == snapshot
        assert ids == {}

    def test_completed_envelope_output_is_aligned_positionally(self):
        ids = {0: "reasoning-added", 1: "fn-added"}
        env = {
            "type": "response.completed",
            "response": {
                "id": "resp-encrypted-again",
                "output": [
                    {"type": "reasoning", "id": "reasoning-final"},
                    {"type": "function_call", "id": "fn-final", "call_id": "call_1"},
                    {"type": "message", "id": "msg-final"},  # never streamed
                ],
            },
        }
        assert normalise_event_ids(env, ids)
        out = env["response"]["output"]
        assert out[0]["id"] == "reasoning-added"
        assert out[1]["id"] == "fn-added"
        assert out[2]["id"] == "msg-final"
        assert ids[2] == "msg-final"
        assert env["response"]["id"] == "resp-encrypted-again"

    def test_completed_envelope_without_output_is_noop(self):
        ids: dict[int, str] = {}
        assert not normalise_event_ids(
            {"type": "response.completed", "response": {}}, ids
        )
        assert not normalise_event_ids({"type": "response.completed"}, ids)


class TestStableIdStream:
    @pytest.mark.asyncio
    async def test_rewrites_events_and_preserves_framing(self):
        raw = b"".join(_sse(e) for e in TOOL_CALL_EVENTS) + b"data: [DONE]\n\n"
        # Split at awkward byte boundaries to exercise buffering.
        chunks = [raw[i : i + 37] for i in range(0, len(raw), 37)]
        wrapper = _StableIdStream(_FakeInner(chunks))

        collected = b"".join([c async for c in wrapper])

        assert collected.endswith(b"data: [DONE]\n\n")
        assert collected.count(b"event: ") == len(TOOL_CALL_EVENTS)
        assert collected.count(b"\n\n") == len(TOOL_CALL_EVENTS) + 1
        events = _parse_events(collected)
        assert [e["type"] for e in events] == [e["type"] for e in TOOL_CALL_EVENTS]
        assert {e.get("item_id") or e["item"]["id"] for e in events} == {"id-added"}
        assert events[-1]["item"]["arguments"] == '{"file_path":"notes.txt"}'

    @pytest.mark.asyncio
    async def test_text_deltas_collapse_onto_one_item(self):
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "message",
                    "id": "m0",
                    "role": "assistant",
                    "content": [],
                },
            },
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "item_id": "e1",
                "delta": "terra",
            },
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "item_id": "e2",
                "delta": " ok",
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {"type": "message", "id": "e3", "role": "assistant"},
            },
        ]
        wrapper = _StableIdStream(_FakeInner([_sse(e) for e in events]))
        parsed = _parse_events(b"".join([c async for c in wrapper]))
        assert [p.get("item_id") or p["item"]["id"] for p in parsed] == ["m0"] * 4
        assert parsed[1]["delta"] == "terra" and parsed[2]["delta"] == " ok"

    @pytest.mark.asyncio
    async def test_non_json_and_non_data_lines_pass_through_verbatim(self):
        raw = b": keepalive\nevent: ping\ndata: not json {\ndata:\n\ndata: [DONE]\n\n"
        wrapper = _StableIdStream(_FakeInner([raw]))
        assert b"".join([c async for c in wrapper]) == raw

    @pytest.mark.asyncio
    async def test_unterminated_tail_is_flushed(self):
        ev = {
            "type": "response.output_text.delta",
            "output_index": 0,
            "item_id": "x",
            "delta": "hi",
        }
        raw = b"data: " + json.dumps(ev).encode()  # no trailing newline
        wrapper = _StableIdStream(_FakeInner([raw]))
        assert b"".join([c async for c in wrapper]) == raw

    @pytest.mark.asyncio
    async def test_non_ascii_survives_rewrite(self):
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": "m0"},
            },
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "item_id": "e1",
                "delta": "héllo ✓",
            },
        ]
        wrapper = _StableIdStream(_FakeInner([_sse(e) for e in events]))
        parsed = _parse_events(b"".join([c async for c in wrapper]))
        assert parsed[1]["delta"] == "héllo ✓"
        assert parsed[1]["item_id"] == "m0"

    @pytest.mark.asyncio
    async def test_aclose_closes_inner(self):
        inner = _FakeInner([])
        wrapper = _StableIdStream(inner)
        await wrapper.aclose()
        assert inner.closed

    def test_is_httpx_async_byte_stream(self):
        assert isinstance(_StableIdStream(_FakeInner([])), httpx.AsyncByteStream)


class TestPatchClientForStableIds:
    def _client_with(self, handler):
        client = httpx.AsyncClient(
            base_url="https://api.githubcopilot.com",
            transport=httpx.MockTransport(handler),
        )
        patch_client_for_stable_ids(client)
        return client

    @pytest.mark.asyncio
    async def test_wraps_streaming_responses_sse(self):
        raw = b"".join(_sse(e) for e in TOOL_CALL_EVENTS)

        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_FakeInner([raw]),
            )

        client = self._client_with(handler)
        req = client.build_request("POST", "/responses", json={"stream": True})
        resp = await client.send(req, stream=True)
        body = b"".join([c async for c in resp.aiter_raw()])
        await resp.aclose()

        parsed = _parse_events(body)
        assert {e.get("item_id") or e["item"]["id"] for e in parsed} == {"id-added"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method, path, ctype",
        [
            ("POST", "/chat/completions", "text/event-stream"),
            ("POST", "/responses", "application/json"),
            ("GET", "/models", "application/json"),
        ],
    )
    async def test_other_traffic_untouched(self, method, path, ctype):
        raw = _sse(TOOL_CALL_EVENTS[1])

        def handler(request):
            return httpx.Response(
                200, headers={"content-type": ctype}, stream=_FakeInner([raw])
            )

        client = self._client_with(handler)
        resp = await client.send(client.build_request(method, path), stream=True)
        body = b"".join([c async for c in resp.aiter_raw()])
        await resp.aclose()
        assert body == raw

    @pytest.mark.asyncio
    async def test_error_responses_untouched(self):
        def handler(request):
            return httpx.Response(
                400,
                headers={"content-type": "text/event-stream"},
                content=b'data: {"error": "nope"}\n\n',
            )

        client = self._client_with(handler)
        resp = await client.send(client.build_request("POST", "/responses"))
        assert resp.status_code == 400
        assert resp.content == b'data: {"error": "nope"}\n\n'
