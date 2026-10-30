"""Safety tests for deletion permission previews."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from code_puppy_core_plugins.file_permission_handler import delete_preview


@pytest.mark.parametrize(
    ("encoding", "bom"),
    [
        ("utf-8", b"\xef\xbb\xbf"),
        ("utf-16-le", b"\xff\xfe"),
        ("utf-16-be", b"\xfe\xff"),
        ("utf-32-le", b"\xff\xfe\x00\x00"),
        ("utf-32-be", b"\x00\x00\xfe\xff"),
    ],
)
def test_supported_bom_text_is_diffed(tmp_path, encoding, bom):
    path = tmp_path / "unicode.txt"
    content = "snowman \r\ncafé\n"
    path.write_bytes(bom + content.encode(encoding))

    preview = delete_preview.preview_delete_file(str(path))

    assert preview is not None
    assert "-snowman \r\n" in preview
    assert "-café\n" in preview
    assert "Text preview omitted" not in preview


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff\xfe\x00",  # truncated UTF-16
        b"\x00\x00\xfe\xff\x00",  # truncated UTF-32
        b"valid text \xff more text",
        b"PK\x03\x04\x14\x00\x00\x00ZIP payload",
        b"text\x00with NUL",
        b"text\x1b[31mterminal control",
        b"text\x7fDEL",
        "text\u0085C1 control".encode(),
        b"line one\rline two",
    ],
)
def test_binary_malformed_or_unsafe_content_is_omitted(tmp_path, payload):
    path = tmp_path / "opaque.bin"
    path.write_bytes(payload)

    preview = delete_preview.preview_delete_file(str(path))

    assert preview is not None
    assert "Text preview omitted" in preview
    assert f"{len(payload)} bytes" in preview
    assert "payload" not in preview
    assert "\x1b" not in preview
    assert "\x7f" not in preview


def test_binary_summary_uses_plugin_translation_catalog(tmp_path):
    from code_puppy.i18n import get_locale, set_locale

    path = tmp_path / "archive.bin"
    path.write_bytes(b"\x00opaque")
    previous_locale = get_locale()
    try:
        set_locale("es")
        preview = delete_preview.preview_delete_file(str(path))
    finally:
        set_locale(previous_locale)

    assert preview is not None
    assert preview.startswith("Archivo binario o no textual")
    assert "7 octetos" in preview


def test_empty_file_has_an_empty_text_diff_not_an_omission(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")

    assert delete_preview.preview_delete_file(str(path)) == ""


def test_diff_escapes_terminal_controls_in_filename():
    assert delete_preview._safe_filename("unsafe\x1bname.txt") == (
        "unsafe\\x1bname.txt"
    )


def test_valid_replacement_character_and_crlf_are_text(tmp_path):
    path = tmp_path / "valid.txt"
    path.write_bytes("replacement: \ufffd\r\n".encode("utf-8"))

    preview = delete_preview.preview_delete_file(str(path))

    assert preview is not None
    assert "-replacement: \ufffd\r\n" in preview
    assert "Text preview omitted" not in preview


def test_missing_or_unreadable_local_file_omits_preview(tmp_path):
    missing = tmp_path / "missing.txt"
    assert delete_preview.preview_delete_file(str(missing)) is None

    path = tmp_path / "unreadable.txt"
    path.write_text("text")
    with patch("builtins.open", side_effect=PermissionError("denied")):
        assert delete_preview.preview_delete_file(str(path)) is None


def test_backend_text_is_used_without_local_disk_or_byte_reconstruction(
    tmp_path, monkeypatch
):
    path = tmp_path / "delegated.txt"
    path.write_bytes(b"local bytes must not be read")
    monkeypatch.setattr(delete_preview, "get_filesystem_backend", lambda: object())
    monkeypatch.setattr(delete_preview, "_is_file", lambda _: True)
    monkeypatch.setattr(delete_preview, "_read_text", lambda _: "host text\n")

    with patch("builtins.open", side_effect=AssertionError("local read")):
        preview = delete_preview.preview_delete_file(str(path))

    assert preview is not None
    assert "-host text\n" in preview
    assert "local bytes" not in preview


@pytest.mark.parametrize(
    "content",
    ["secret\x00payload", "surrogate\udcff", "C1\u0085control", "escape\x1b[31m"],
)
def test_backend_unsafe_text_is_summarized_without_content(
    tmp_path, monkeypatch, content
):
    path = tmp_path / "delegated.bin"
    path.write_bytes(b"local bytes")
    monkeypatch.setattr(delete_preview, "get_filesystem_backend", lambda: object())
    monkeypatch.setattr(delete_preview, "_is_file", lambda _: True)
    monkeypatch.setattr(delete_preview, "_read_text", lambda _: content)

    preview = delete_preview.preview_delete_file(str(path))

    assert preview == "Binary or non-text file. Text preview omitted."
    assert "secret" not in preview
    assert "surrogate" not in preview
    assert "escape" not in preview
    assert "\x1b" not in preview


def test_older_core_without_backend_api_uses_strict_local_classifier(
    tmp_path, monkeypatch
):
    path = tmp_path / "older-core.bin"
    path.write_bytes(b"bad \xff bytes")
    monkeypatch.setattr(delete_preview, "get_filesystem_backend", None)
    monkeypatch.setattr(delete_preview, "_is_file", None)
    monkeypatch.setattr(delete_preview, "_read_text", None)

    preview = delete_preview.preview_delete_file(str(path))

    assert preview is not None
    assert "Text preview omitted" in preview
    assert "bad" not in preview


@pytest.mark.asyncio
async def test_registered_delete_callback_omits_binary_and_denial_preserves_file(
    tmp_path, monkeypatch
):
    from code_puppy import callbacks
    from code_puppy_core_plugins.file_permission_handler import register_callbacks

    path = tmp_path / "archive.zip"
    original = b"PK\x03\x04\x00\x01unsafe archive bytes"
    path.write_bytes(original)
    captured: dict[str, object] = {}

    async def deny_with_feedback(
        file_path, operation, preview=None, message_group=None
    ):
        captured.update(
            file_path=file_path,
            operation=operation,
            preview=preview,
            message_group=message_group,
        )
        return False, "keep this file"

    monkeypatch.setattr(
        register_callbacks, "prompt_for_file_permission_async", deny_with_feedback
    )
    registered = callbacks.get_callbacks("file_permission")
    assert register_callbacks.handle_file_permission_async in registered

    from code_puppy.tools.file_modifications import _delete_file_async

    result = await _delete_file_async(None, str(path))

    assert result.get("success") is False
    assert "user rejected" in result.get("message", "").lower()
    assert captured["operation"] == "delete"
    assert "Text preview omitted" in captured["preview"]
    assert "unsafe archive bytes" not in captured["preview"]
    assert path.read_bytes() == original
    assert "keep this file" in result["message"]


@pytest.mark.asyncio
async def test_registered_delete_callback_approval_keeps_normal_delete_flow(
    tmp_path, monkeypatch
):
    from code_puppy.tools.file_modifications import _delete_file_async
    from code_puppy_core_plugins.file_permission_handler import register_callbacks

    path = tmp_path / "ordinary.txt"
    path.write_bytes(b"ordinary text\n")
    captured: dict[str, object] = {}

    async def approve(file_path, operation, preview=None, message_group=None):
        captured.update(operation=operation, preview=preview)
        return True, None

    monkeypatch.setattr(register_callbacks, "prompt_for_file_permission_async", approve)

    result = await _delete_file_async(None, str(path))

    assert result.get("success") is True
    assert captured["operation"] == "delete"
    assert "-ordinary text\n" in captured["preview"]
    assert not path.exists()
