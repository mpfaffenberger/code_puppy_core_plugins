"""HWND-only Windows Graphics Capture; never activates or captures the desktop.

The backend must validate window identity, policy and current DWM bounds before
and after calling this adapter. Bounds and geometry are physical screen pixels
(not logical/DPI-scaled coordinates), despite CaptureGeometry's macOS names.
"""

from __future__ import annotations

import math
import threading
from pathlib import Path
from typing import Any

from .backend_types import ComputerUseError
from .geometry import CaptureGeometry, Rect

MAX_CAPTURE_PIXELS = 32_000_000


def _validate(info: dict, timeout: float) -> tuple[int, Rect]:
    if info.get("minimized"):
        raise ComputerUseError(
            "Cannot capture a minimized window. Restore it without activating "
            "it, then retry the capture."
        )
    try:
        hwnd = info["window_id"]
        if isinstance(hwnd, bool) or not isinstance(hwnd, int) or hwnd <= 0:
            raise ValueError("window_id must be a positive HWND integer")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        left, top, right, bottom = info["bounds"]
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or int(value) != value
            for value in (left, top, right, bottom)
        ):
            raise ValueError("bounds must be integral physical pixels")
        width, height = int(right - left), int(bottom - top)
        if width <= 0 or height <= 0 or width * height > MAX_CAPTURE_PIXELS:
            raise ValueError("bounds must be positive and at most 32 million pixels")
        if int(info["pid"]) <= 0:
            raise ValueError("pid must be positive")
        return hwnd, Rect(float(left), float(top), width, height)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ComputerUseError(f"Invalid window capture request: {exc}") from exc


def _copy_frame(frame: Any, bounds: Rect) -> bytes:
    width, height = frame.width, frame.height
    if (width, height) != (bounds.width, bounds.height):
        raise ComputerUseError(
            f"WGC frame {width}x{height} does not match the visible DWM bounds "
            f"{bounds.width}x{bounds.height}. Refresh window bounds and retry; "
            "capture will not crop or scale an ambiguous frame."
        )
    buffer = frame.frame_buffer
    if buffer.shape != (height, width, 4) or str(buffer.dtype) != "uint8":
        raise ComputerUseError("WGC returned an invalid BGRA pixel buffer.")
    # WGC rows may be padded. tobytes packs the view and owns the pixels before
    # the native mapped frame is released at the end of the callback.
    pixels = buffer.tobytes(order="C")
    if len(pixels) != width * height * 4:
        raise ComputerUseError("WGC returned an incomplete BGRA pixel buffer.")
    return pixels


def _capture_pixels(hwnd: int, bounds: Rect, timeout: float) -> bytes:
    ready = threading.Event()
    finished = threading.Event()
    cancelled = threading.Event()
    lock = threading.Lock()
    outcome: dict[str, Any] = {}

    def on_frame_arrived(frame: Any, control: Any) -> None:
        # Nothing in a callback writes a file, including late timeout callbacks.
        with lock:
            try:
                if not ready.is_set() and not cancelled.is_set():
                    outcome["pixels"] = _copy_frame(frame, bounds)
            except Exception as exc:  # noqa: BLE001 - native capture/cleanup error boundary.
                outcome["error"] = exc
            finally:
                try:
                    control.stop()
                except Exception as exc:  # noqa: BLE001 - native capture/cleanup error boundary.
                    outcome["error"] = exc
                ready.set()

    def on_closed() -> None:
        with lock:
            if not ready.is_set():
                outcome["error"] = ComputerUseError(
                    "The requested window closed before WGC delivered a frame."
                )
                ready.set()

    def run() -> None:
        control = None
        try:
            # Lazy import keeps this module importable on non-Windows hosts.
            from windows_capture import WindowsCapture

            capture = WindowsCapture(
                window_hwnd=hwnd, cursor_capture=False, draw_border=True
            )
            capture.event(on_frame_arrived)
            capture.event(on_closed)
            control = capture.start_free_threaded()
            ready.wait()
        except Exception as exc:  # noqa: BLE001 - native capture/cleanup error boundary.
            outcome["error"] = exc
        finally:
            if control is not None:
                try:
                    control.stop()
                    control.wait()
                except Exception as exc:  # noqa: BLE001 - native capture/cleanup error boundary.
                    outcome["error"] = exc
            finished.set()

    # The native stop/wait API has no timeout (stop may itself join). Never run
    # it on the caller thread. A stalled driver can retain this daemon worker,
    # but the caller returns on time and late frames cannot publish screenshots.
    worker = threading.Thread(target=run, name="hwnd-wgc-capture", daemon=True)
    worker.start()
    if not finished.wait(timeout):
        cancelled.set()
        ready.set()
        raise ComputerUseError(
            "Windows Graphics Capture timed out waiting for a frame or native "
            "cleanup. Check that the window is open, restored and capturable."
        )
    if "error" in outcome:
        error = outcome["error"]
        raise ComputerUseError(f"Windows Graphics Capture failed: {error}") from error
    if "pixels" not in outcome:
        raise ComputerUseError("Windows Graphics Capture returned no frame.")
    return outcome["pixels"]


def _save_png(target: Path, pixels: bytes, bounds: Rect) -> None:
    from PIL import Image

    image = Image.frombytes(
        "RGBA", (int(bounds.width), int(bounds.height)), pixels, "raw", "BGRA"
    )
    created = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation also closes the race after the initial path check.
        with target.open("xb") as output:
            created = True
            image.save(output, format="PNG")
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise
    finally:
        image.close()


def capture_window(info: dict, target: Path, timeout: float = 8) -> dict[str, Any]:
    """Capture only info['window_id']; refuse minimized/misaligned captures.

    timeout bounds the native lifecycle, including startup and stop/wait. No
    foreground changes, desktop crops, PrintWindow or silent fallback are used.
    Existing output paths are never overwritten; output is always PNG.
    """
    hwnd, bounds = _validate(info, timeout)
    try:
        target = target.expanduser().absolute()
        if target.exists() or target.is_symlink():
            raise ComputerUseError(f"Screenshot path already exists: {target}")
        pixels = _capture_pixels(hwnd, bounds, timeout)
        _save_png(target, pixels, bounds)
    except ComputerUseError:
        raise
    except Exception as exc:
        raise ComputerUseError(f"Windows window capture failed: {exc}") from exc
    return {
        "success": True,
        "path": str(target),
        "application": str(info.get("process") or info.get("executable") or ""),
        "bundle_id": str(info.get("executable") or ""),
        "pid": int(info["pid"]),
        "window_id": hwnd,
        "window_title": str(info.get("title") or ""),
        "geometry": CaptureGeometry(bounds, int(bounds.width), int(bounds.height), 1.0),
    }
