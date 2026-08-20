"""Safe subprocess adapter for the optional Pup Lint executable."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

MAX_DIAGNOSTICS = 1_000
TIMEOUT_SECONDS = 120


def command_prefix() -> list[str] | None:
    """Return an argv prefix for Pup Lint without invoking a shell."""
    executable = shutil.which("pup-lint")
    if executable:
        return [executable]
    try:
        installed = importlib.util.find_spec("pup_lint") is not None
    except (ImportError, ValueError):
        installed = False
    return [sys.executable, "-m", "pup_lint"] if installed else None


def available() -> bool:
    return command_prefix() is not None


async def lint_paths(
    paths: list[str],
    *,
    cwd: str | None = None,
    line_length: int | None = None,
    select: list[str] | None = None,
    ignore: list[str] | None = None,
) -> dict[str, Any]:
    prefix = command_prefix()
    if prefix is None:
        return {
            "success": False,
            "error": "pup-lint is not installed in the Code Puppy environment or on PATH.",
        }
    if line_length is not None and line_length < 1:
        return {"success": False, "error": "line_length must be positive."}

    working_directory = Path(cwd or ".").expanduser().resolve()
    if not working_directory.is_dir():
        return {"success": False, "error": f"Not a directory: {working_directory}"}

    argv = [*prefix, "--format", "json"]
    if line_length is not None:
        argv.extend(("--line-length", str(line_length)))
    if select:
        argv.extend(("--select", ",".join(select)))
    if ignore:
        argv.extend(("--ignore", ",".join(ignore)))
    argv.extend(paths or ["."])

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=working_directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        if "process" in locals():
            await _stop_process(process)
        raise
    except TimeoutError:
        if "process" in locals():
            await _stop_process(process)
        return {
            "success": False,
            "error": f"pup-lint timed out after {TIMEOUT_SECONDS} seconds.",
        }
    except OSError as error:
        return {"success": False, "error": f"Could not run pup-lint: {error}"}

    stderr_text = stderr.decode(errors="replace").strip()
    if process.returncode not in (0, 1):
        return {
            "success": False,
            "exit_code": process.returncode,
            "error": stderr_text or "pup-lint failed without an error message.",
        }
    try:
        diagnostics = json.loads(stdout.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return {
            "success": False,
            "exit_code": process.returncode,
            "error": f"pup-lint returned invalid JSON: {error}",
        }
    if not isinstance(diagnostics, list):
        return {"success": False, "error": "pup-lint returned a non-list JSON result."}

    count = len(diagnostics)
    return {
        "success": True,
        "clean": count == 0,
        "count": count,
        "diagnostics": diagnostics[:MAX_DIAGNOSTICS],
        "truncated": count > MAX_DIAGNOSTICS,
    }


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        pass
    await process.wait()
