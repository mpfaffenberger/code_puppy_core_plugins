"""Parse ACP prompt content blocks into a Code Puppy prompt.

The client delivers a prompt as a list of typed content blocks. The SDK parses
the wire into pydantic models before our ``prompt`` handler runs, so we work
with objects (``TextContentBlock``, ``ImageContentBlock``,
``EmbeddedResourceContentBlock``, …) rather than raw dicts.

We split a prompt into three parts that map onto ``run_with_mcp``'s signature:

* **text** — text blocks, embedded text resources (spliced in under a header,
  honouring the advertised ``embeddedContext`` capability), resource-link
  references, and a visible notice for embedded blob resources the model
  cannot take.
* **attachments** — image blocks carrying base64 data → pydantic-ai
  ``BinaryContent`` (honouring the advertised ``image`` capability), and
  embedded blob resources whose media type the current model accepts.
* **link_attachments** — image blocks carrying only a URI → ``ImageUrl``.

Blocks are duck-typed (``getattr``) so a newer SDK adding fields never breaks
parsing. Audio is not advertised, so audio blocks degrade to a text note.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from typing import Any, List, Sequence

logger = logging.getLogger(__name__)


@dataclass
class ParsedPrompt:
    """A prompt split into text + multimodal attachments for ``run_with_mcp``."""

    text: str = ""
    attachments: List[Any] = field(default_factory=list)  # BinaryContent
    link_attachments: List[Any] = field(default_factory=list)  # ImageUrl


def parse_prompt(
    blocks: List[Any], media_types: Sequence[str] | None = None
) -> ParsedPrompt:
    """Split a list of ACP content blocks into text + attachments.

    ``media_types`` lists the binary media types the current model accepts;
    embedded blobs outside it become a text notice instead of an attachment.
    """
    parts: List[str] = []
    result = ParsedPrompt()
    supported = tuple(media_types or ())
    for block in blocks or []:
        text = _render_block(block, result, supported)
        if text:
            parts.append(text)
    result.text = "\n\n".join(parts)
    return result


def flatten_prompt(blocks: List[Any]) -> str:
    """Back-compat text-only view (used where attachments aren't needed)."""
    return parse_prompt(blocks).text


def _render_block(block: Any, result: ParsedPrompt, media_types: Sequence[str]) -> str:
    block_type = getattr(block, "type", None)

    if block_type == "text":
        return getattr(block, "text", "") or ""

    if block_type == "resource":
        return _render_embedded_resource(
            getattr(block, "resource", None), result, media_types
        )

    if block_type == "resource_link":
        uri = getattr(block, "uri", "") or ""
        name = getattr(block, "name", "") or uri
        return f"[Referenced resource: {name} ({uri})]" if uri else ""

    if block_type == "image":
        _collect_image(block, result)
        return ""

    if block_type == "audio":
        return "[Audio attached]"

    # Unknown/newer block type: fall back to any ``text`` it carries.
    return getattr(block, "text", "") or ""


def _collect_image(block: Any, result: ParsedPrompt) -> None:
    """Turn an image block into a BinaryContent (data) or ImageUrl (uri)."""
    data = getattr(block, "data", None)
    mime = getattr(block, "mime_type", None) or "image/png"
    if data:
        try:
            result.attachments.append(_make_binary(data, mime))
            return
        except Exception:  # noqa: BLE001
            logger.debug("ACP: could not decode image data", exc_info=True)
    uri = getattr(block, "uri", None)
    if uri:
        try:
            from pydantic_ai import ImageUrl

            result.link_attachments.append(ImageUrl(url=uri))
        except Exception:  # noqa: BLE001
            logger.debug("ACP: could not build ImageUrl", exc_info=True)


def _make_binary(data_b64: str, media_type: str) -> Any:
    """Build a BinaryContent from base64 text; raises on bad data.

    Lazy import keeps pydantic-ai off this module's import path.
    """
    from pydantic_ai import BinaryContent

    return BinaryContent(data=base64.b64decode(data_b64), media_type=media_type)


def _render_embedded_resource(
    resource: Any, result: ParsedPrompt, media_types: Sequence[str]
) -> str:
    """Render an embedded resource: text splices in, blobs become attachments or a notice."""
    if resource is None:
        return ""
    text = getattr(resource, "text", None)
    if text:
        uri = getattr(resource, "uri", "") or "embedded"
        return f"<context uri={uri!r}>\n{text}\n</context>"
    blob = getattr(resource, "blob", None)
    if not blob:
        return ""
    return _collect_embedded_blob(resource, blob, result, media_types)


def _collect_embedded_blob(
    resource: Any, blob: str, result: ParsedPrompt, media_types: Sequence[str]
) -> str:
    """Attach a blob the model accepts; otherwise return a notice naming its uri.

    A supported media type whose base64 fails to decode gets its own notice so
    the user can tell a corrupt payload from an unsupported one.
    """
    mime = getattr(resource, "mime_type", None)
    uri = getattr(resource, "uri", None) or "embedded resource"
    if mime and mime in media_types:
        try:
            result.attachments.append(_make_binary(blob, mime))
            return ""
        except Exception as exc:  # noqa: BLE001
            # Type only: the exception message can echo blob bytes.
            logger.debug("ACP: could not decode embedded blob: %s", type(exc).__name__)
            return f"[Attachment not sent: {uri} ({mime}) could not be decoded.]"
    return (
        f"[Attachment not sent: {uri} ({mime or 'unknown type'}) "
        "is not supported by the current model.]"
    )
