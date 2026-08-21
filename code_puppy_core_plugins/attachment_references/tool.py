"""Materialize the latest user-attached images into reference files.

Pasted or dragged images arrive as ``BinaryContent`` stapled onto the user
message; they never touch the filesystem. Tools such as ``codex_imagegen``
condition on ``reference_images`` *paths*, so there is otherwise no way to point
image generation at something the user just pasted. This tool bridges that gap
by writing the latest turn's image attachments to disk and returning their
paths -- nothing more (SRP/YAGNI).
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any, List, Sequence

from pydantic_ai import BinaryContent, RunContext
from pydantic_ai.messages import UserPromptPart

from code_puppy import config

TOOL_NAME = "save_attachments_as_references"


def _is_image(item: Any) -> bool:
    return isinstance(item, BinaryContent) and str(
        getattr(item, "media_type", "") or ""
    ).startswith("image/")


def _extract_latest_user_images(messages: Sequence[Any] | None) -> List[BinaryContent]:
    """Return image attachments from the most recent user message only.

    Walks history backwards to the last request carrying a ``UserPromptPart``
    (an actual user turn, not a tool return) and collects its image parts in
    order. Returns an empty list when the latest user turn has no images.
    """
    if not messages:
        return []
    for message in reversed(list(messages)):
        parts = getattr(message, "parts", None)
        if not parts:
            continue
        user_parts = [part for part in parts if isinstance(part, UserPromptPart)]
        if not user_parts:
            continue
        images: List[BinaryContent] = []
        for part in user_parts:
            content = part.content
            if isinstance(content, str):
                continue
            for item in content:
                if _is_image(item):
                    images.append(item)
        return images
    return []


def _extension_for(media_type: str | None) -> str:
    guessed = mimetypes.guess_extension(media_type or "") if media_type else None
    if guessed in {".jpe", ".jpeg"}:
        return ".jpg"
    return guessed or ".png"


def _save_reference_images(images: Sequence[BinaryContent]) -> List[Path]:
    """Write each image to the Code Puppy data dir and return the paths."""
    if not images:
        return []
    output_dir = Path(config.DATA_DIR) / "attachment_references"
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    saved: List[Path] = []
    for image in images:
        extension = _extension_for(getattr(image, "media_type", None))
        output_path = output_dir / f"attachment-{uuid.uuid4().hex}{extension}"
        temporary_path = output_dir / f"{output_path.name}.tmp"
        try:
            temporary_path.write_bytes(image.data)
            os.chmod(temporary_path, 0o600)
            temporary_path.replace(output_path)
        except OSError:
            temporary_path.unlink(missing_ok=True)
            raise
        saved.append(output_path)
    return saved


def register_save_attachments_as_references(agent: Any) -> None:
    """Register the attachment-to-reference bridge on a pydantic-ai agent."""

    @agent.tool
    async def save_attachments_as_references(context: RunContext) -> dict[str, Any]:
        """Save images the user attached in their latest message to disk.

        Use this to turn a pasted or dragged image into a real file path so
        other tools can act on it. In particular, pass the returned paths to
        ``codex_imagegen`` as ``reference_images`` to generate a new image that
        preserves the subject or style of what the user just shared.

        Only images from the most recent user message are saved. Returns the
        saved file paths; the paths are stable on disk under the Code Puppy
        data directory.
        """
        images = _extract_latest_user_images(getattr(context, "messages", None))
        if not images:
            return {
                "success": True,
                "paths": [],
                "count": 0,
                "message": (
                    "No image attachments were found in the latest user message."
                ),
            }
        try:
            paths = await asyncio.to_thread(_save_reference_images, images)
        except OSError as exc:
            return {
                "success": False,
                "error": f"Could not save reference images: {exc}",
            }
        return {
            "success": True,
            "paths": [str(path) for path in paths],
            "count": len(paths),
        }


def register_tools_callback() -> list[dict[str, Any]]:
    return [
        {
            "name": TOOL_NAME,
            "register_func": register_save_attachments_as_references,
        }
    ]
