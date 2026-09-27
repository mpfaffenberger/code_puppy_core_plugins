"""Tests for the attachment-to-reference bridge tool."""

import asyncio
from types import SimpleNamespace

from pydantic_ai import BinaryContent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)

from code_puppy import config
from code_puppy_core_plugins.attachment_references import register_callbacks
from code_puppy_core_plugins.attachment_references import tool as attachment_tool


def _png(marker: bytes) -> BinaryContent:
    return BinaryContent(data=b"\x89PNG" + marker, media_type="image/png")


def test_extract_returns_only_latest_user_turn_images():
    first = _png(b"first")
    second = _png(b"second")
    history = [
        ModelRequest(parts=[UserPromptPart(content=["earlier", first])]),
        ModelResponse(parts=[TextPart(content="ok")]),
        ModelRequest(parts=[UserPromptPart(content=["latest", second])]),
    ]

    images = attachment_tool._extract_latest_user_images(history)

    assert images == [second]


def test_extract_skips_tool_returns_and_non_image_binaries():
    image = _png(b"keep")
    document = BinaryContent(data=b"%PDF-1.4", media_type="application/pdf")
    history = [
        ModelRequest(parts=[UserPromptPart(content=["look", image, document])]),
        ModelResponse(parts=[TextPart(content="thinking")]),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="grep",
                    content="no images here",
                    tool_call_id="call-1",
                )
            ]
        ),
    ]

    images = attachment_tool._extract_latest_user_images(history)

    assert images == [image]


def test_extract_handles_empty_and_text_only_history():
    assert attachment_tool._extract_latest_user_images(None) == []
    assert attachment_tool._extract_latest_user_images([]) == []
    text_only = [ModelRequest(parts=[UserPromptPart(content="just words")])]
    assert attachment_tool._extract_latest_user_images(text_only) == []


def test_extension_for_maps_common_image_types():
    assert attachment_tool._extension_for("image/png") == ".png"
    assert attachment_tool._extension_for("image/jpeg") == ".jpg"
    assert attachment_tool._extension_for(None) == ".png"
    assert attachment_tool._extension_for("image/unknownxyz") == ".png"


def test_save_reference_images_writes_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    images = [_png(b"a"), BinaryContent(data=b"jpegbytes", media_type="image/jpeg")]

    paths = attachment_tool._save_reference_images(images)

    assert len(paths) == 2
    assert paths[0].read_bytes() == b"\x89PNGa"
    assert paths[0].suffix == ".png"
    assert paths[1].suffix == ".jpg"
    assert all(path.parent == tmp_path / "attachment_references" for path in paths)
    assert not any(path.name.endswith(".tmp") for path in paths)


def _register_tool():
    registered = {}

    class FakeAgent:
        def tool(self, function):
            registered[function.__name__] = function
            return function

    attachment_tool.register_save_attachments_as_references(FakeAgent())
    return registered["save_attachments_as_references"]


def test_tool_returns_paths_for_latest_attachment(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    save = _register_tool()
    context = SimpleNamespace(
        messages=[ModelRequest(parts=[UserPromptPart(content=["hi", _png(b"z")])])]
    )

    result = asyncio.run(save(context))

    assert result["success"] is True
    assert result["count"] == 1
    assert len(result["paths"]) == 1
    assert result["paths"][0].endswith(".png")


def test_tool_reports_when_no_images_present(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    save = _register_tool()
    context = SimpleNamespace(
        messages=[ModelRequest(parts=[UserPromptPart(content="text only")])]
    )

    result = asyncio.run(save(context))

    assert result == {
        "success": True,
        "paths": [],
        "count": 0,
        "message": "No image attachments were found in the latest user message.",
    }


def test_registration_contract():
    tools = register_callbacks._register_tools()
    assert tools == [
        {
            "name": "save_attachments_as_references",
            "register_func": attachment_tool.register_save_attachments_as_references,
        }
    ]
    assert register_callbacks._advertise_tool("code-puppy") == [
        "save_attachments_as_references"
    ]
    assert register_callbacks._advertise_tool() == ["save_attachments_as_references"]
