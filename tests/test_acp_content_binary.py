"""Embedded blob resources: attach when the model accepts the media type, else notice."""

from __future__ import annotations

import base64
import logging
from types import SimpleNamespace

import pytest
from acp.schema import (
    BlobResourceContents,
    EmbeddedResourceContentBlock,
    TextContentBlock,
    TextResourceContents,
)
from pydantic_ai import BinaryContent

from code_puppy_core_plugins.acp.content import flatten_prompt, parse_prompt

PDF_BYTES = b"%PDF-1.4 fake pdf body"
PDF_B64 = base64.b64encode(PDF_BYTES).decode()
PDF_URI = "file:///reports/q3.pdf"
PDF_MIME = "application/pdf"
PDF_NOTICE = (
    f"[Attachment not sent: {PDF_URI} ({PDF_MIME}) "
    "is not supported by the current model.]"
)
# 11 base64 chars is never valid padding, so decoding raises binascii.Error.
INVALID_B64 = "SECRETMARK1"


def _blob_block(
    blob: str = PDF_B64, mime: str | None = PDF_MIME, uri: str = PDF_URI
) -> EmbeddedResourceContentBlock:
    resource = BlobResourceContents(blob=blob, mimeType=mime, uri=uri)
    return EmbeddedResourceContentBlock(type="resource", resource=resource)


def _text_block(text: str) -> TextContentBlock:
    return TextContentBlock(type="text", text=text)


def test_supported_blob_becomes_binary_attachment():
    parsed = parse_prompt([_blob_block()], [PDF_MIME])

    assert len(parsed.attachments) == 1
    attachment = parsed.attachments[0]
    assert isinstance(attachment, BinaryContent)
    assert attachment.data == PDF_BYTES
    assert attachment.media_type == PDF_MIME
    assert parsed.text == ""
    assert parsed.link_attachments == []


@pytest.mark.parametrize("media_types", [None, [], ["image/png"]])
def test_unsupported_blob_yields_notice_not_attachment(media_types):
    parsed = parse_prompt([_blob_block()], media_types)

    assert parsed.attachments == []
    assert parsed.text == PDF_NOTICE


def test_notice_prefers_resource_name_over_uri():
    resource = SimpleNamespace(
        blob=PDF_B64, mime_type=PDF_MIME, uri=PDF_URI, name="Q3 report"
    )
    block = SimpleNamespace(type="resource", resource=resource)

    parsed = parse_prompt([block], None)

    assert parsed.text == (
        f"[Attachment not sent: Q3 report ({PDF_MIME}) "
        "is not supported by the current model.]"
    )


def test_missing_mime_type_is_reported_as_unknown_type():
    parsed = parse_prompt([_blob_block(mime=None)], [PDF_MIME])

    assert parsed.attachments == []
    assert parsed.text == (
        f"[Attachment not sent: {PDF_URI} (unknown type) "
        "is not supported by the current model.]"
    )


def test_invalid_base64_yields_notice_without_raising():
    parsed = parse_prompt([_blob_block(blob=INVALID_B64)], [PDF_MIME])

    assert parsed.attachments == []
    assert parsed.text == PDF_NOTICE


@pytest.mark.parametrize("media_types", [None, [PDF_MIME]])
def test_text_resource_splices_context_regardless_of_media_types(media_types):
    resource = TextResourceContents(
        text="hello", uri="file:///notes.md", mimeType="text/markdown"
    )
    block = EmbeddedResourceContentBlock(type="resource", resource=resource)

    parsed = parse_prompt([block], media_types)

    assert parsed.text == "<context uri='file:///notes.md'>\nhello\n</context>"
    assert parsed.attachments == []


def test_mixed_prompt_keeps_text_order_one_attachment_one_notice():
    blocks = [
        _text_block("before"),
        _blob_block(uri="file:///a.pdf"),
        _blob_block(mime="image/png", uri="file:///b.png"),
        _text_block("after"),
    ]

    parsed = parse_prompt(blocks, [PDF_MIME])

    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].media_type == PDF_MIME
    assert parsed.text == (
        "before\n\n"
        "[Attachment not sent: file:///b.png (image/png) "
        "is not supported by the current model.]\n\n"
        "after"
    )


def test_logs_never_contain_blob_content(caplog):
    marker_blob = "MARKERAAAA" + INVALID_B64
    caplog.set_level(logging.DEBUG)

    parsed = parse_prompt([_blob_block(blob=marker_blob)], [PDF_MIME])

    assert "MARKER" not in parsed.text
    assert "MARKER" not in caplog.text
    decode_records = [
        record
        for record in caplog.records
        if "could not decode embedded blob" in record.getMessage()
    ]
    assert [record.getMessage() for record in decode_records] == [
        "ACP: could not decode embedded blob: Error"
    ]
    assert all(record.exc_info is None for record in decode_records)


def test_flatten_prompt_returns_notice_for_blob():
    assert flatten_prompt([_blob_block()]) == PDF_NOTICE
