"""Safe, text-only previews for file deletion permission prompts."""

from __future__ import annotations

import difflib
import os
from importlib.resources import files

try:
    from code_puppy.i18n import t

    try:
        from code_puppy.i18n import register_plugin_catalog
    except ImportError:  # pragma: no cover - older Code Puppy versions
        from code_puppy.i18n import add_catalog_dir

        add_catalog_dir(str(files(__package__).joinpath("locales")))
    else:
        register_plugin_catalog(files(__package__).joinpath("locales"))
except ImportError:  # pragma: no cover - older Code Puppy versions
    t = None

_CATALOG_NAMESPACE = "plugin.file-permission-handler.delete_preview"

try:
    from code_puppy.tools.io_backends import get_filesystem_backend
except ImportError:  # pragma: no cover - older Code Puppy versions
    get_filesystem_backend = None

try:
    from code_puppy.tools.fs_access import is_file as _is_file
    from code_puppy.tools.fs_access import read_text as _read_text
except ImportError:  # pragma: no cover - older Code Puppy versions
    _is_file = None
    _read_text = None


_BOMS = (
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\xff\xfe", "utf-16-le"),
)


def _decode_text(raw: bytes) -> str | None:
    """Decode supported text encodings without replacing invalid bytes."""
    encoding = "utf-8"
    bom_length = 0
    for marker, candidate in _BOMS:
        if raw.startswith(marker):
            encoding = candidate
            bom_length = len(marker)
            break
    try:
        return raw[bom_length:].decode(encoding, errors="strict")
    except UnicodeDecodeError:
        return None


def _is_safe_text(content: str) -> bool:
    """Reject terminal controls, NULs, surrogate code points, and bare CRs."""
    for index, character in enumerate(content):
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            return False
        if codepoint < 0x20 and character not in "\t\n\r":
            return False
        if 0x7F <= codepoint <= 0x9F:
            return False
        if character == "\r" and (
            index + 1 == len(content) or content[index + 1] != "\n"
        ):
            return False
    return True


def _safe_filename(file_path: str) -> str:
    """Escape control and surrogate characters in untrusted file names."""
    escaped: list[str] = []
    for character in os.path.basename(file_path):
        codepoint = ord(character)
        if codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            escaped.append(f"\\x{codepoint:02x}")
        elif 0xD800 <= codepoint <= 0xDFFF:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(character)
    return "".join(escaped)


def _omitted_preview(size: int | None = None) -> str:
    """Describe an omitted preview without rendering file contents."""
    if t is None:
        size_text = f" ({size} bytes)" if size is not None else ""
        return f"Binary or non-text file{size_text}. Text preview omitted."
    if size is None:
        return t(f"{_CATALOG_NAMESPACE}.omitted")
    return t(f"{_CATALOG_NAMESPACE}.omitted_size", size=size)


def preview_delete_file(file_path: str, *, context_lines: int = 3) -> str | None:
    """Return a safe text diff or a binary/non-text omission summary.

    Local files are classified from raw bytes. When Code Puppy has an active
    filesystem backend, use its text-only API and never fall back to local disk
    or try to reconstruct bytes from the backend's text.
    """
    file_path = os.path.abspath(file_path)
    try:
        if get_filesystem_backend is not None and get_filesystem_backend() is not None:
            if _is_file is None or _read_text is None or not _is_file(file_path):
                return None
            original = _read_text(file_path)
            if not isinstance(original, str):
                return None
            if not _is_safe_text(original):
                return _omitted_preview()
        else:
            if not os.path.isfile(file_path):
                return None
            with open(file_path, "rb") as source:
                raw = source.read()
            original = _decode_text(raw)
            if original is None or not _is_safe_text(original):
                return _omitted_preview(len(raw))

        filename = _safe_filename(file_path)
        return "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                [],
                fromfile=f"a/{filename}",
                tofile=f"b/{filename}",
                n=context_lines,
            )
        )
    except Exception:
        return None
