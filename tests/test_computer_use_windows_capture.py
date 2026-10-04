"""Mocked WGC events only: never capture a user's native windows."""

import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PIL import Image

from code_puppy_core_plugins.computer_use import windows_capture as adapter
from code_puppy_core_plugins.computer_use.backend_types import ComputerUseError
from code_puppy_core_plugins.computer_use.geometry import CaptureGeometry, Rect


@pytest.fixture
def info():
    return {
        "window_id": 12345,
        "pid": 42,
        "title": "Disposable test window",
        "executable": r"C:\Apps\fixture.exe",
        "process": "fixture.exe",
        "bounds": [-10, 20, -8, 21],
        "minimized": False,
    }


@pytest.fixture
def frame():
    # Non-contiguous rows, just like native mapped textures with row padding.
    storage = np.zeros((1, 4, 4), dtype=np.uint8)
    storage[0, 0] = [3, 2, 1, 255]
    storage[0, 1] = [30, 20, 10, 128]
    return SimpleNamespace(width=2, height=1, frame_buffer=storage[:, :2])


@pytest.fixture
def native(monkeypatch, frame):
    state = SimpleNamespace(
        mode="frame",
        frame=frame,
        internal=Mock(),
        control=Mock(),
        callbacks={},
        kwargs=None,
        started=threading.Event(),
        release=threading.Event(),
        stopped=threading.Event(),
    )

    def stop():
        state.stopped.set()
        if state.mode == "blocked_stop":
            state.release.wait(2)
        if state.mode == "stop_error":
            raise RuntimeError("stop failed")

    state.control.stop.side_effect = stop

    class FakeCapture:
        def __init__(self, **kwargs):
            state.kwargs = kwargs
            if state.mode == "constructor_error":
                raise RuntimeError("WGC unavailable")

        def event(self, handler):
            state.callbacks[handler.__name__] = handler

        def start_free_threaded(self):
            state.started.set()
            if state.mode == "start_error":
                raise RuntimeError("native start failed")
            if state.mode == "blocked_start":
                state.release.wait(2)
            if state.mode == "closed":
                state.callbacks["on_closed"]()
            elif state.mode not in ("timeout", "blocked_start"):
                state.callbacks["on_frame_arrived"](state.frame, state.internal)
                # A normal close after a frame must not invalidate that frame.
                state.callbacks["on_closed"]()
            return state.control

    monkeypatch.setitem(
        sys.modules, "windows_capture", SimpleNamespace(WindowsCapture=FakeCapture)
    )
    yield state
    state.release.set()


def test_capture_contract_png_and_physical_geometry(tmp_path, info, native):
    target = tmp_path / "nested" / "not-a-png.extension"
    result = adapter.capture_window(info, target)
    assert result == {
        "success": True,
        "path": str(target.absolute()),
        "application": "fixture.exe",
        "bundle_id": info["executable"],
        "pid": 42,
        "window_id": 12345,
        "window_title": info["title"],
        "geometry": CaptureGeometry(Rect(-10, 20, 2, 1), 2, 1, 1.0),
    }
    assert result["geometry"].screenshot_to_quartz(1, 0) == (-9, 20)
    assert native.kwargs == {
        "window_hwnd": 12345,
        "cursor_capture": False,
        "draw_border": True,
    }
    with Image.open(target) as image:
        assert image.format == "PNG"
        assert image.size == (2, 1)
        assert image.getpixel((0, 0)) == (1, 2, 3, 255)
        assert image.getpixel((1, 0)) == (10, 20, 30, 128)
    native.internal.stop.assert_called_once()
    native.control.stop.assert_called_once()
    native.control.wait.assert_called_once()


@pytest.mark.parametrize(
    "mode,match",
    [
        ("closed", "closed before"),
        ("constructor_error", "WGC unavailable"),
        ("start_error", "native start failed"),
        ("stop_error", "stop failed"),
    ],
)
def test_native_errors(tmp_path, info, native, mode, match):
    native.mode = mode
    with pytest.raises(ComputerUseError, match=match):
        adapter.capture_window(info, tmp_path / "capture.png", timeout=0.5)
    assert not (tmp_path / "capture.png").exists()
    if mode in ("closed", "stop_error"):
        native.control.stop.assert_called_once()


@pytest.mark.parametrize("mode", ["timeout", "blocked_start", "blocked_stop"])
def test_timeout_is_bounded_and_late_callbacks_cannot_save(
    tmp_path, info, native, mode
):
    native.mode = mode
    target = tmp_path / "capture.png"
    start = time.monotonic()
    with pytest.raises(ComputerUseError, match="timed out"):
        adapter.capture_window(info, target, timeout=0.05)
    assert time.monotonic() - start < 1
    assert native.started.is_set()
    native.release.set()
    assert native.stopped.wait(1)
    native.callbacks["on_frame_arrived"](native.frame, native.internal)
    assert not target.exists()


@pytest.mark.parametrize(
    "change,match",
    [
        ({"width": 3}, "does not match"),
        ({"height": 33_000_000}, "does not match"),
        ({"frame_buffer": np.zeros((1, 2, 3), dtype=np.uint8)}, "invalid BGRA"),
        ({"frame_buffer": np.zeros((1, 2, 4), dtype=np.float32)}, "invalid BGRA"),
    ],
)
def test_invalid_frames(tmp_path, info, native, change, match):
    for key, value in change.items():
        setattr(native.frame, key, value)
    with pytest.raises(ComputerUseError, match=match):
        adapter.capture_window(info, tmp_path / "capture.png")
    native.internal.stop.assert_called_once()
    native.control.stop.assert_called_once()


def test_callback_copy_failure(tmp_path, info, native):
    native.frame.frame_buffer = SimpleNamespace(
        shape=(1, 2, 4),
        dtype="uint8",
        tobytes=Mock(side_effect=RuntimeError("copy failed")),
    )
    with pytest.raises(ComputerUseError, match="copy failed"):
        adapter.capture_window(info, tmp_path / "capture.png")
    native.control.stop.assert_called_once()


def test_callback_stop_failure(tmp_path, info, native):
    native.internal.stop.side_effect = RuntimeError("callback stop failed")
    with pytest.raises(ComputerUseError, match="callback stop failed"):
        adapter.capture_window(info, tmp_path / "capture.png")
    native.control.stop.assert_called_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"minimized": True},
        {"window_id": 0},
        {"window_id": True},
        {"window_id": "123"},
        {"pid": 0},
        {"bounds": [0, 0, 0, 2]},
        {"bounds": [0, 0, -1, 2]},
        {"bounds": [0, 0, 8001, 4000]},
        {"bounds": [0.5, 0, 2, 1]},
        {"bounds": [0, 0, float("inf"), 1]},
        {"bounds": [0, 1]},
        {"bounds": None},
    ],
)
def test_invalid_requests_fail_before_native(tmp_path, info, native, changes):
    info.update(changes)
    with pytest.raises(ComputerUseError):
        adapter.capture_window(info, tmp_path / "capture.png")
    assert native.kwargs is None


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), None])
def test_invalid_timeout(tmp_path, info, native, timeout):
    with pytest.raises(ComputerUseError, match="Invalid window capture request"):
        adapter.capture_window(info, tmp_path / "capture.png", timeout=timeout)
    assert native.kwargs is None


def test_pixel_limit_boundary(info):
    info["bounds"] = [0, 0, 8000, 4000]
    assert adapter._validate(info, 8)[1] == Rect(0, 0, 8000, 4000)


def test_existing_output_never_overwritten(tmp_path, info, native):
    target = tmp_path / "capture.png"
    target.write_bytes(b"existing")
    with pytest.raises(ComputerUseError, match="already exists"):
        adapter.capture_window(info, target)
    assert target.read_bytes() == b"existing"
    assert native.kwargs is None


def test_exclusive_create_closes_path_race(tmp_path, info, native, monkeypatch):
    target = tmp_path / "capture.png"
    original = adapter._save_png

    def race(path, pixels, bounds):
        path.write_bytes(b"competitor")
        original(path, pixels, bounds)

    monkeypatch.setattr(adapter, "_save_png", race)
    with pytest.raises(ComputerUseError):
        adapter.capture_window(info, target)
    assert target.read_bytes() == b"competitor"


def test_save_failure_removes_partial_output(tmp_path, info, native, monkeypatch):
    def fail(image, output, **kwargs):
        output.write(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(Image.Image, "save", fail)
    target = tmp_path / "capture.png"
    with pytest.raises(ComputerUseError, match="disk full"):
        adapter.capture_window(info, target)
    assert not target.exists()


def test_frame_bytes_are_owned_after_callback(tmp_path, info, native):
    native.control.stop.side_effect = lambda: native.frame.frame_buffer.fill(0)
    target = tmp_path / "capture.png"
    adapter.capture_window(info, target)
    with Image.open(target) as image:
        assert image.getpixel((0, 0)) == (1, 2, 3, 255)


def test_incomplete_pixel_buffer(tmp_path, info, native):
    native.frame.frame_buffer = SimpleNamespace(
        shape=(1, 2, 4), dtype="uint8", tobytes=lambda **kwargs: b"short"
    )
    with pytest.raises(ComputerUseError, match="incomplete BGRA"):
        adapter.capture_window(info, tmp_path / "capture.png")


def test_native_wait_failure(tmp_path, info, native):
    native.control.wait.side_effect = RuntimeError("join failed")
    with pytest.raises(ComputerUseError, match="join failed"):
        adapter.capture_window(info, tmp_path / "capture.png")
    assert not (tmp_path / "capture.png").exists()


def test_missing_native_dependency(tmp_path, info, monkeypatch):
    monkeypatch.setitem(sys.modules, "windows_capture", None)
    with pytest.raises(ComputerUseError, match="Windows Graphics Capture failed"):
        adapter.capture_window(info, tmp_path / "capture.png")
