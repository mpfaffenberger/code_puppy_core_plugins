"""Backend contract/safety tests without touching the real Windows desktop."""

import threading
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from code_puppy_core_plugins.computer_use.backend import MacOSBackend, create_backend
from code_puppy_core_plugins.computer_use.backend_types import ComputerUseError
from code_puppy_core_plugins.computer_use.batch import run_batch
from code_puppy_core_plugins.computer_use.geometry import CaptureGeometry, Rect
from code_puppy_core_plugins.computer_use.policy import PolicyStore
from code_puppy_core_plugins.computer_use.state import StateStore
from code_puppy_core_plugins.computer_use.windows_backend import WindowsBackend
from code_puppy_core_plugins.computer_use.windows_runtime import WindowsRuntime


class Native:
    def __init__(self):
        self.info = {
            "window_id": 42,
            "pid": 7,
            "title": "Editor",
            "process": "editor.exe",
            "executable": r"C:\Apps\editor.exe",
            "bounds": [-1000, 50, -400, 650],
            "minimized": False,
        }
        self.focus = 42
        self.stop = False
        self.events = []
        self.user32 = SimpleNamespace(GetForegroundWindow=lambda: self.focus)

    def physical_pixels(self):
        return nullcontext()

    def desktop_ready(self):
        pass

    def emergency_stop(self):
        return self.stop

    def window_info(self, hwnd):
        if hwnd != 42:
            raise ComputerUseError("Unknown window")
        return deepcopy(self.info)

    def list_windows(self):
        return [deepcopy(self.info)]

    def foreground(self, hwnd):
        self.focus = hwnd

    def assert_foreground(self, hwnd):
        if self.focus != hwnd:
            raise ComputerUseError("Focus changed")

    def check_point(self, hwnd, x, y):
        assert hwnd == 42

    def move(self, x, y):
        self.events.append(("move", x, y))

    def mouse_event(self, flag, data=0):
        self.events.append(("mouse", flag, data))

    def key_event(self, code, up=False, unicode=False):
        self.events.append(("key", code, up, unicode))


@pytest.fixture
def backend(tmp_path):
    policy = PolicyStore(tmp_path / "policy.json")
    policy.set_enabled(True)
    native = Native()
    ax = Mock()
    ax.snapshot.return_value = (
        [{"id": 1, "role": "Edit", "actions": ["UIAScroll"]}],
        {1: "element"},
        {
            "scanned_node_count": 1,
            "truncated": False,
            "scan_limit_reached": False,
            "overflow_summary": {},
        },
    )
    for name in ("click", "set_value", "perform_action", "select_text", "scroll"):
        getattr(ax, name).return_value = {"success": True}

    def capture(info, target):
        left, top, right, bottom = info["bounds"]
        return {
            "success": True,
            "path": str(target),
            "geometry": CaptureGeometry(
                Rect(left, top, right - left, bottom - top),
                right - left,
                bottom - top,
                1,
            ),
        }

    return WindowsBackend(native, ax, capture, policy, StateStore())


def revision(backend):
    return backend.get_app_state("editor")["state_revision"]


def test_platform_selector_is_lazy():
    assert isinstance(create_backend("darwin"), MacOSBackend)
    backend = create_backend("win32")
    assert isinstance(backend, WindowsRuntime)
    assert backend._backend is None
    backend._executor.shutdown()


@pytest.mark.parametrize(
    "app",
    ["editor", "editor.exe", "Editor", "hwnd:42", "hwnd:0x2a", r"C:\Apps\editor.exe"],
)
def test_resolve_exact_target(backend, app):
    result = backend.get_app_state(app)
    assert result["window_id"] == 42
    assert result["application"] == "hwnd:42"
    assert (
        result["action_coordinate_system"] == "top-left, global Windows physical pixels"
    )


def test_ambiguous_target_rejected(backend):
    backend.native.list_windows = lambda: [
        backend.native.info,
        {**backend.native.info, "window_id": 43},
    ]
    with pytest.raises(ComputerUseError, match="Ambiguous"):
        backend.get_app_state("editor.exe")


@pytest.mark.parametrize("limit", [0, 501, True, 1.5])
def test_invalid_tree_limit(backend, limit):
    with pytest.raises(ComputerUseError, match="max_nodes"):
        backend.get_app_state("Editor", limit)


def test_stale_revisions_and_plain_snapshot_invalidation(backend):
    first = revision(backend)
    second = revision(backend)
    with pytest.raises(ComputerUseError, match="Stale"):
        backend.click(first, 1)
    backend.snapshot("editor")
    with pytest.raises(ComputerUseError, match="Stale"):
        backend.click(second, 1)


def test_single_use(backend):
    state = revision(backend)
    assert backend.click(state, 1)["success"]
    with pytest.raises(ComputerUseError, match="changed"):
        backend.click(state, 1)


@pytest.mark.parametrize(
    "field,value",
    [("pid", 8), ("bounds", [0, 0, 600, 600]), ("executable", r"C:\Other\editor.exe")],
)
def test_identity_or_geometry_change(backend, field, value):
    state = revision(backend)
    backend.native.info[field] = value
    with pytest.raises(ComputerUseError, match="changed"):
        backend.click(state, 1)
    backend.accessibility.click.assert_not_called()


def test_expiry(backend):
    state = revision(backend)
    backend.states.current().created_at -= 121
    with pytest.raises(ComputerUseError, match="expired"):
        backend.click(state, 1)


def test_pause_and_process_deny_rechecked(backend):
    state = revision(backend)
    backend.policy.set_paused(True)
    with pytest.raises(ComputerUseError, match="paused"):
        backend.click(state, 1)
    backend.policy.set_paused(False)
    backend.policy.deny("editor.exe")
    with pytest.raises(ComputerUseError, match="denied"):
        backend.click(state, 1)


def test_hard_security_process_denied(backend):
    backend.native.info["process"] = "consent.exe"
    with pytest.raises(ComputerUseError, match="security"):
        backend.get_app_state("hwnd:42")


def test_emergency_stop(backend):
    state = revision(backend)
    backend.native.stop = True
    with pytest.raises(ComputerUseError, match="Emergency"):
        backend.click(state, 1)
    assert backend.policy.status()["paused"]
    assert backend.states.current() is None


def test_cancellation_prevents_input(backend):
    state = revision(backend)
    backend.cancelled.set()
    with pytest.raises(ComputerUseError, match="cancelled"):
        backend.press_key(state, "enter")
    assert not backend.native.events


def test_semantic_operations_delegate_without_pixel_fallback(backend):
    cases = [
        ("set_value", (1, "hello")),
        ("perform_action", (1, "UIAInvoke")),
        ("select_text", (1, "dog", "cursor_after", "blue ", None)),
        ("scroll_pages", ("down", 0.5)),
    ]
    for method, args in cases:
        result = getattr(backend, method)(revision(backend), *args)
        assert result["success"]
    backend.accessibility.set_value.assert_called_with("element", "hello")
    backend.accessibility.select_text.assert_called_with(
        "element", "dog", "cursor_after", "blue ", None
    )
    backend.accessibility.scroll.assert_called_with("element", "down", 0.5)
    assert not backend.native.events


def test_semantic_failure_invalidates_state(backend):
    state = revision(backend)
    backend.accessibility.click.side_effect = ComputerUseError("No pattern")
    with pytest.raises(ComputerUseError, match="No pattern"):
        backend.click(state, 1, consume=False)
    assert backend.states.current() is None
    assert not backend.native.events


def test_pixel_coordinates_negative_origin(backend):
    backend.click_pixel(revision(backend), 20, 30)
    assert ("move", -980, 80) in backend.native.events


def test_out_of_bounds_never_moves_pointer(backend):
    with pytest.raises(ComputerUseError, match="outside"):
        backend.click_pixel(revision(backend), 600, 30)
    assert not backend.native.events


def test_background_capture_never_activates(backend):
    backend.native.focus = 99
    backend.screenshot(app_name="editor")
    assert backend.native.focus == 99
    with pytest.raises(ComputerUseError, match="app_name"):
        backend.screenshot()


def test_successful_batch_same_engine(backend):
    state = revision(backend)
    result = run_batch(
        backend,
        state,
        [
            {"action": "set_value", "element_id": 1, "value": "new"},
            {"action": "perform_action", "element_id": 1, "action_name": "UIASetFocus"},
            {"action": "wait", "seconds": 0},
        ],
        lambda *args: {"settled": True},
    )
    assert result["success"]
    assert result["state_revision"] != state
    assert len(result["completed_steps"]) == 3
    backend.accessibility.perform_action.assert_called_once_with(
        "element", "UIASetFocus"
    )


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), -1, 11])
def test_batch_invalid_wait_fails(backend, seconds):
    result = run_batch(
        backend, revision(backend), [{"action": "wait", "seconds": seconds}], Mock()
    )
    assert not result["success"]


def test_unknown_batch_action(backend):
    result = run_batch(backend, revision(backend), [{"action": "unknown"}], Mock())
    assert not result["success"]


def test_key_balance_and_unicode(backend):
    backend.press_key(revision(backend), "a", ["control"])
    assert backend.native.events == [
        ("key", 17, False, False),
        ("key", 65, False, False),
        ("key", 65, True, False),
        ("key", 17, True, False),
    ]
    backend.native.events.clear()
    backend.type_text(revision(backend), "{\U0001f436}")
    assert [event[1] for event in backend.native.events if not event[2]] == [
        123,
        0xD83D,
        0xDC36,
        125,
    ]


def test_runtime_uses_one_thread_for_whole_request():
    runtime = WindowsRuntime()
    runtime._backend = SimpleNamespace(states=StateStore())
    try:
        threads = [
            runtime.run_request(threading.get_ident, (), {}, threading.Event())
            for _ in range(3)
        ]
        assert len(set(threads)) == 1
        assert threads[0] != threading.get_ident()
    finally:
        runtime._executor.shutdown()
