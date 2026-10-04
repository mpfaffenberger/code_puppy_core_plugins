"""Windows implementation of the existing computer-use backend contract."""

from __future__ import annotations

import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import windows_input
from .backend_types import ComputerUseError
from .policy import policy_store
from .state import state_store


class WindowsBackend:
    def __init__(
        self,
        native=None,
        accessibility=None,
        capture=None,
        policy=None,
        states=None,
        cancelled=None,
    ):
        self._native = native
        self._accessibility = accessibility
        self._capture = capture
        self.policy = policy or policy_store
        self.states = states or state_store
        self.cancelled = cancelled or threading.Event()
        self._scroll_ids = []

    def invalidate_state(self):
        self.states.clear()

    @property
    def native(self):
        if self._native is None:
            from . import windows_native

            self._native = windows_native
        return self._native

    @property
    def accessibility(self):
        if self._accessibility is None:
            from .windows_accessibility import WindowsAccessibility

            self._accessibility = WindowsAccessibility()
        return self._accessibility

    def _check(self):
        self.policy.require_enabled()
        if self.cancelled.is_set():
            self.states.clear()
            raise ComputerUseError("Computer Use request cancelled")
        self.native.desktop_ready()
        if self.native.emergency_stop():
            self.policy.set_paused(True)
            self.states.clear()
            raise ComputerUseError("Emergency stop: run /computer-use resume when safe")

    def _info(self, hwnd):
        self._check()
        info = self.native.window_info(hwnd)
        self.policy.require(info["process"])
        self.policy.require(info["executable"])
        return info

    def _resolve(self, app_name):
        self._check()
        if not app_name:
            return self._info(self.native.user32.GetForegroundWindow())
        if app_name.lower().startswith("hwnd:"):
            try:
                return self._info(int(app_name[5:], 0))
            except ValueError as exc:
                raise ComputerUseError(
                    "Use hwnd: followed by a decimal or hexadecimal HWND"
                ) from exc
        wanted = app_name.casefold()
        matches = [
            item
            for item in self.native.list_windows()
            if wanted
            in {
                item["title"].casefold(),
                item["process"].casefold(),
                item["process"].casefold().removesuffix(".exe"),
                item["executable"].casefold(),
            }
        ]
        if not matches:
            raise ComputerUseError(f"Running application/window not found: {app_name}")
        for info in matches:
            self.policy.require(info["process"])
            self.policy.require(info["executable"])
        if len(matches) > 1:
            choices = [
                {"app_name": f"hwnd:{item['window_id']}", "title": item["title"]}
                for item in matches[:20]
            ]
            raise ComputerUseError(
                f"Ambiguous application; select an exact window: {choices}"
            )
        return matches[0]

    def _tree(self, info, max_nodes):
        if (
            isinstance(max_nodes, bool)
            or not isinstance(max_nodes, int)
            or not 1 <= max_nodes <= 500
        ):
            raise ComputerUseError("max_nodes must be an integer from 1 to 500")
        nodes, elements, metadata = self.accessibility.snapshot(
            info["window_id"], max_nodes
        )
        self._scroll_ids = [
            node["id"] for node in nodes if "UIAScroll" in node.get("actions", [])
        ]
        return nodes, elements, metadata

    def snapshot(self, app_name=None, max_nodes=500, preferred_window_id=None):
        with self.native.physical_pixels():
            info = self._resolve(
                f"hwnd:{preferred_window_id}" if preferred_window_id else app_name
            )
            self.states.clear()
            nodes, _, metadata = self._tree(info, max_nodes)
            self._info(info["window_id"])
            return {
                "success": True,
                "application": f"hwnd:{info['window_id']}",
                "node_count": len(nodes),
                "nodes": nodes,
                **metadata,
                "warning": "Element IDs require a revision from computer_get_app_state before actions.",
            }

    def _capture_window(self, info, path=None):
        if path is None:
            target = (
                Path(tempfile.gettempdir())
                / f"code-puppy-windows-{uuid.uuid4().hex}.png"
            )
        else:
            target = Path(path).expanduser()
        if self._capture is None:
            from .windows_capture import capture_window

            self._capture = capture_window
        return self._capture(info, target)

    def get_app_state(self, app_name, max_nodes=500):
        if not app_name:
            raise ComputerUseError("app_name is required")
        with self.native.physical_pixels():
            info = self._resolve(app_name)
            self.states.clear()
            # Match macOS: activate before reading accessibility for providers
            # (notably Electron) that expose their full tree only when focused.
            self.native.foreground(info["window_id"])
            info = self._info(info["window_id"])
            capture = self._capture_window(info)
            nodes, elements, metadata = self._tree(info, max_nodes)
            current = self._info(info["window_id"])
            if any(
                current[key] != info[key] for key in ("pid", "executable", "bounds")
            ):
                raise ComputerUseError("Window changed during snapshot; retry")
            state = self.states.create(
                application=f"hwnd:{info['window_id']}",
                bundle_id=info["process"].casefold(),
                pid=info["pid"],
                window_id=info["window_id"],
                window_title=info["title"],
                geometry=capture["geometry"],
                screenshot_path=str(capture["path"]),
                elements=elements,
            )
            self._identity = (info["pid"], info["executable"], info["bounds"])
            result = {
                "success": True,
                **state.public_metadata(),
                "node_count": len(nodes),
                "nodes": nodes,
                **metadata,
                "warning": "Use this state_revision for one mutation or one guarded batch, then fetch fresh state.",
            }
            result["action_coordinate_system"] = (
                "top-left, global Windows physical pixels"
            )
            result["platform"] = "windows"
            return result

    def require_state(self, revision, *, consume=False):
        with self.native.physical_pixels():
            state = self.states.require(revision)
            info = self._info(state.window_id)
            self._verify_identity(state, info)
            self.native.foreground(state.window_id)
            self._guard(state)
            return self.states.require(revision, consume=consume)

    def _verify_identity(self, state, info):
        rect = state.geometry.window_points
        expected = [rect.x, rect.y, rect.x + rect.width, rect.y + rect.height]
        if info["pid"] != state.pid or info["bounds"] != expected:
            raise ComputerUseError(
                "Window identity or geometry changed; fetch fresh app state"
            )
        identity = getattr(self, "_identity", None)
        if identity and (info["pid"], info["executable"], info["bounds"]) != identity:
            raise ComputerUseError("Target process changed; fetch fresh app state")

    def _guard(self, state):
        self._verify_identity(state, self._info(state.window_id))
        self.native.assert_foreground(state.window_id)

    @contextmanager
    def _mutation(self, revision, consume):
        with self.native.physical_pixels():
            state = self.require_state(revision, consume=consume)
            try:
                yield state
            except Exception:
                self.states.clear()
                raise

    def _element(self, state, element_id):
        element = state.elements.get(element_id)
        if element is None:
            raise ComputerUseError(f"Unknown or expired element ID {element_id}")
        return element

    def click(self, state_revision, element_id, *, consume=True):
        with self._mutation(state_revision, consume) as state:
            return self.accessibility.click(self._element(state, element_id))

    def set_value(self, state_revision, element_id, value, *, consume=True):
        with self._mutation(state_revision, consume) as state:
            return self.accessibility.set_value(self._element(state, element_id), value)

    def perform_action(self, state_revision, element_id, action, *, consume=True):
        with self._mutation(state_revision, consume) as state:
            return self.accessibility.perform_action(
                self._element(state, element_id), action
            )

    def select_text(
        self,
        state_revision,
        element_id,
        text,
        mode="text",
        prefix=None,
        suffix=None,
        *,
        consume=True,
    ):
        with self._mutation(state_revision, consume) as state:
            return self.accessibility.select_text(
                self._element(state, element_id), text, mode, prefix, suffix
            )

    def press_key(self, state_revision, key, modifiers=None, *, consume=True):
        with self._mutation(state_revision, consume) as state:
            windows_input.press_key(
                self.native, key, modifiers, lambda: self._guard(state)
            )
            return {"success": True, "key": key, "modifiers": modifiers or []}

    def type_text(self, state_revision, text, *, consume=True):
        with self._mutation(state_revision, consume) as state:
            windows_input.type_text(self.native, text, lambda: self._guard(state))
            return {"success": True, "characters": len(text)}

    @staticmethod
    def _point(state, x, y):
        return tuple(
            round(value) for value in state.geometry.screenshot_to_quartz(x, y)
        )

    def click_pixel(
        self, state_revision, x, y, button="left", click_count=1, *, consume=True
    ):
        with self._mutation(state_revision, consume) as state:
            windows_input.click(
                self.native,
                state.window_id,
                self._point(state, x, y),
                button,
                click_count,
                lambda: self._guard(state),
            )
            return {
                "success": True,
                "x": x,
                "y": y,
                "button": button,
                "click_count": click_count,
            }

    def drag_pixel(
        self,
        state_revision,
        start_x,
        start_y,
        end_x,
        end_y,
        duration=0.5,
        *,
        consume=True,
    ):
        with self._mutation(state_revision, consume) as state:
            windows_input.drag(
                self.native,
                state.window_id,
                self._point(state, start_x, start_y),
                self._point(state, end_x, end_y),
                duration,
                lambda: self._guard(state),
            )
            return {"success": True}

    def scroll_pages(self, state_revision, direction, pages=1.0, *, consume=True):
        with self._mutation(state_revision, consume) as state:
            if not self._scroll_ids:
                raise ComputerUseError(
                    "Window exposes no UIA ScrollPattern; semantic page scrolling unavailable"
                )
            # Prefer the innermost exposed scroll container in the focused window.
            element = self._element(state, self._scroll_ids[-1])
            return self.accessibility.scroll(element, direction, pages)

    def screenshot(self, path=None, app_name=None):
        if not app_name:
            raise ComputerUseError(
                "app_name is required; whole-desktop capture would bypass application policy"
            )
        with self.native.physical_pixels():
            info = self._resolve(app_name)
            capture = self._capture_window(info, path)
            current = self._info(info["window_id"])
            if current["pid"] != info["pid"] or current["bounds"] != info["bounds"]:
                raise ComputerUseError("Window changed during capture; retry")
            geometry = capture.pop("geometry")
            return {
                **capture,
                **geometry.as_dict(),
                "action_coordinate_system": "top-left, global Windows physical pixels",
            }
