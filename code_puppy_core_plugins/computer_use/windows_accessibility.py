"""Semantic UIA operations; no input injection or coordinate fallbacks.

The backend owns a COM-initialized serial worker. Construct on any thread, but
call every public method (and use returned wrappers) only on that worker.
pywinauto is imported lazily there. COM initialization/teardown belongs to the
worker, not this adapter. Snapshot IDs are valid only for that snapshot.

Live smoke tests: use a standard Windows EDIT and RichEdit containing
``atarget b target!``. Select with prefix/suffix, then before/after; verify the
caret visually and with TextPattern.GetSelection. Some legacy EDIT providers
lack TextPattern: unsupported is intentional. Also test ES_PASSWORD, disabled
and read-only controls, and a scrollable multiline RichEdit at fractional pages.
"""

from __future__ import annotations

import math
import threading
from collections import Counter, deque
from functools import wraps

from .backend_types import ComputerUseError

MAX_SCAN_NODES = 2000
MAX_TEXT_MATCHES = 2000
START, END = 0, 1
CHARACTER = 0
NO_SCROLL = -1.0


def _operation(function):
    @wraps(function)
    def checked(self, *args, **kwargs):
        self._check_thread()
        try:
            return function(self, *args, **kwargs)
        except ComputerUseError:
            raise
        except Exception:  # noqa: BLE001 - COM/provider exception types vary.
            # Provider exception messages can contain protected text.
            raise ComputerUseError(
                "UI Automation operation failed; refresh the snapshot."
            ) from None

    return checked


def _pattern(element, name):
    try:
        return getattr(element, "iface_" + name)
    except Exception:  # noqa: BLE001 - COM/provider exception types vary.
        return None


def _property(element, name, default=None):
    try:
        return getattr(element.element_info.element, name)
    except Exception:  # noqa: BLE001 - COM/provider exception types vary.
        return default


class WindowsAccessibility:
    """Thread-affine adapter for pywinauto UIA wrappers."""

    def __init__(self):
        self._thread_id = None
        self._protected = set()

    def _check_thread(self):
        current = threading.get_ident()
        if self._thread_id is None:
            self._thread_id = current
        elif self._thread_id != current:
            raise ComputerUseError("UIA wrappers must stay on their COM worker thread.")

    def _root(self, hwnd):
        try:
            from pywinauto import Desktop
        except ImportError:
            raise ComputerUseError(
                "Windows accessibility requires pywinauto."
            ) from None
        return Desktop(backend="uia").window(handle=hwnd).wrapper_object()

    def _children(self, element):
        # Walk lazily: children() would materialize an unbounded provider tree.
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo

        walker = IUIA().iuia.ControlViewWalker
        child = walker.GetFirstChildElement(element.element_info.element)
        while child:
            yield UIAWrapper(UIAElementInfo(child))
            child = walker.GetNextSiblingElement(child)

    def _password(self, element):
        # Unknown password state fails closed, rather than exposing provider text.
        return id(element) in self._protected or bool(
            _property(element, "CurrentIsPassword", True)
        )

    def _guard(self, element, *, text=False):
        if not _property(element, "CurrentIsEnabled", False):
            raise ComputerUseError("The UIA element is disabled or unavailable.")
        if text and self._password(element):
            raise ComputerUseError(
                "Text operations on protected elements are forbidden."
            )

    def _actions(self, element, protected):
        if not _property(element, "CurrentIsEnabled", False):
            return []
        actions = []
        for pattern, action in (
            ("invoke", "UIAInvoke"),
            ("toggle", "UIAToggle"),
            ("selection_item", "UIASelect"),
        ):
            if _pattern(element, pattern) is not None:
                actions.append(action)
        expand = _pattern(element, "expand_collapse")
        if expand is not None and expand.CurrentExpandCollapseState != 3:
            actions.extend(["UIAExpand", "UIACollapse"])
        if _property(element, "CurrentIsKeyboardFocusable", False):
            actions.append("UIASetFocus")
        if not protected:
            value = _pattern(element, "value")
            if value is None:
                value = _pattern(element, "range_value")
            if value is not None and not value.CurrentIsReadOnly:
                actions.append("UIASetValue")
            text = _pattern(element, "text")
            if text is not None and text.SupportedTextSelection != 0:
                actions.append("UIASelectText")
        scroll = _pattern(element, "scroll")
        if scroll is not None and (
            scroll.CurrentHorizontallyScrollable or scroll.CurrentVerticallyScrollable
        ):
            actions.append("UIAScroll")
        return actions

    def _node(self, element, node_id, parent_id, protected):
        value = None
        if not protected:
            pattern = _pattern(element, "value")
            if pattern is None:
                pattern = _pattern(element, "range_value")
            if pattern is not None:
                value = str(pattern.CurrentValue)[:500]
        node = {
            "id": node_id,
            "parent_id": parent_id,
            "role": str(element.element_info.control_type or "Unknown"),
            "title": "" if protected else str(_property(element, "CurrentName", "")),
            "description": ""
            if protected
            else str(_property(element, "CurrentHelpText", "")),
            "value": None if protected else value,
            "enabled": bool(_property(element, "CurrentIsEnabled", False)),
            "focused": bool(_property(element, "CurrentHasKeyboardFocus", False)),
            "actions": self._actions(element, protected),
        }
        try:
            rect = element.element_info.rectangle
            node["bounds"] = {
                "x": rect.left,
                "y": rect.top,
                "width": rect.right - rect.left,
                "height": rect.bottom - rect.top,
            }
        except Exception:  # noqa: BLE001 - bounds are optional provider data.
            return node
        return node

    @_operation
    def snapshot(
        self, hwnd: int, max_nodes: int
    ) -> tuple[list[dict], dict[int, object], dict]:
        if isinstance(hwnd, bool) or not isinstance(hwnd, int) or hwnd <= 0:
            raise ComputerUseError("hwnd must be a positive integer.")
        if (
            isinstance(max_nodes, bool)
            or not isinstance(max_nodes, int)
            or not 1 <= max_nodes <= 500
        ):
            raise ComputerUseError("max_nodes must be an integer from 1 to 500.")
        self._protected.clear()
        queue = deque([(self._root(hwnd), None, False)])
        nodes, elements, excluded = [], {}, Counter()
        scanned, limited = 0, False
        seen = set()
        while queue and scanned < MAX_SCAN_NODES:
            element, parent_id, inherited = queue.popleft()
            scanned += 1
            runtime_id = _property(element, "CurrentRuntimeId")
            # pywinauto caches the UIA runtime ID on element_info.
            runtime_id = getattr(element.element_info, "runtime_id", runtime_id)
            identity = tuple(runtime_id) if runtime_id else id(element)
            if identity in seen:
                continue
            seen.add(identity)
            protected = inherited or self._password(element)
            retained_id = None
            if len(nodes) < max_nodes:
                retained_id = len(nodes) + 1
                if protected:
                    self._protected.add(id(element))
                nodes.append(self._node(element, retained_id, parent_id, protected))
                elements[retained_id] = element
            else:
                excluded[str(element.element_info.control_type or "Unknown")] += 1
            # Queue + processed nodes never exceeds the examination budget.
            for child in self._children(element):
                if scanned + len(queue) >= MAX_SCAN_NODES:
                    limited = True
                    break
                queue.append((child, retained_id, protected))
        limited = limited or bool(queue)
        return (
            nodes,
            elements,
            {
                "scanned_node_count": scanned,
                "returned_node_count": len(nodes),
                "truncated": bool(excluded or limited),
                "scan_limit_reached": limited,
                "overflow_summary": {
                    "excluded_node_count": sum(excluded.values()),
                    "roles": dict(excluded.most_common(12)),
                },
            },
        )

    @_operation
    def perform_action(self, element: object, action: str) -> dict:
        self._guard(element)
        available = self._actions(element, self._password(element))
        matched = next(
            (
                name
                for name in available
                if isinstance(action, str) and name.casefold() == action.casefold()
            ),
            None,
        )
        if matched is None:
            raise ComputerUseError("The requested UIA action is not supported.")
        if matched in {"UIASetValue", "UIASelectText", "UIAScroll"}:
            raise ComputerUseError(
                "Use the corresponding value, text, or scroll method with arguments."
            )
        if matched == "UIASetFocus":
            # wrapper.set_focus may inject input; invoke the COM method directly.
            element.element_info.element.SetFocus()
        else:
            pattern, method = {
                "UIAInvoke": ("invoke", "Invoke"),
                "UIAToggle": ("toggle", "Toggle"),
                "UIASelect": ("selection_item", "Select"),
                "UIAExpand": ("expand_collapse", "Expand"),
                "UIACollapse": ("expand_collapse", "Collapse"),
            }[matched]
            getattr(_pattern(element, pattern), method)()
        return {"success": True, "action": matched, "state_invalidated": True}

    @_operation
    def click(self, element: object) -> dict:
        self._guard(element)
        for pattern, action in (
            ("invoke", "UIAInvoke"),
            ("selection_item", "UIASelect"),
            ("toggle", "UIAToggle"),
        ):
            if _pattern(element, pattern) is not None:
                return self.perform_action(element, action)
        raise ComputerUseError(
            "No semantic Invoke, SelectionItem, or Toggle pattern is available."
        )

    @_operation
    def set_value(self, element: object, value: str) -> dict:
        self._guard(element, text=True)
        if not isinstance(value, str):
            raise ComputerUseError("value must be a string.")
        pattern = _pattern(element, "value")
        numeric = pattern is None
        if numeric:
            pattern = _pattern(element, "range_value")
        if pattern is None or pattern.CurrentIsReadOnly:
            raise ComputerUseError(
                "The element has no writable Value or RangeValue pattern."
            )
        if numeric:
            try:
                value = float(value)
            except ValueError:
                raise ComputerUseError("RangeValue requires a finite number.") from None
            if (
                not math.isfinite(value)
                or not pattern.CurrentMinimum <= value <= pattern.CurrentMaximum
            ):
                raise ComputerUseError("RangeValue is outside the permitted range.")
        pattern.SetValue(value)
        return {"success": True, "action": "UIASetValue", "state_invalidated": True}

    def _find_range(self, document, text, prefix, suffix):
        search = document.Clone()
        matches = []
        for _ in range(MAX_TEXT_MATCHES):
            found = search.FindText(text, False, False)
            if not found:
                break
            before, after = document.Clone(), document.Clone()
            before.MoveEndpointByRange(END, found, START)
            after.MoveEndpointByRange(START, found, END)
            if (
                found.GetText(-1) == text
                and (prefix is None or before.GetText(-1).endswith(prefix))
                and (suffix is None or after.GetText(-1).startswith(suffix))
            ):
                matches.append(found.Clone())
                if len(matches) > 1:
                    raise ComputerUseError(
                        "Text selection is ambiguous: multiple exact matches."
                    )
            # Advance one UIA character from the match start, preserving overlaps.
            # Never pass Python string offsets as UTF-16 endpoint offsets.
            previous = search.Clone()
            search.MoveEndpointByRange(START, found, START)
            moved = search.MoveEndpointByUnit(START, CHARACTER, 1)
            if not moved or search.CompareEndpoints(START, previous, START) <= 0:
                raise ComputerUseError("UIA text search did not advance safely.")
            if search.CompareEndpoints(START, document, END) >= 0:
                break
        else:
            raise ComputerUseError(
                "Text search limit reached; uniqueness cannot be verified."
            )
        if len(matches) != 1:
            raise ComputerUseError("Text selection found no exact matching range.")
        return matches[0]

    @_operation
    def select_text(
        self,
        element: object,
        text: str,
        mode: str = "text",
        prefix: str | None = None,
        suffix: str | None = None,
    ) -> dict:
        self._guard(element, text=True)
        modes = {
            "text": "text",
            "before": "before",
            "after": "after",
            "cursor_before": "before",
            "cursor_after": "after",
        }
        if not isinstance(mode, str) or mode not in modes:
            raise ComputerUseError("Unsupported text selection mode.")
        if not isinstance(text, str) or not text:
            raise ComputerUseError("Text selection requires nonempty text.")
        if any(
            item is not None and not isinstance(item, str) for item in (prefix, suffix)
        ):
            raise ComputerUseError("Text context must be a string or None.")
        pattern = _pattern(element, "text")
        if pattern is None or pattern.SupportedTextSelection == 0:
            raise ComputerUseError("The element has no selectable TextPattern.")
        selected = self._find_range(pattern.DocumentRange, text, prefix, suffix)
        if modes[mode] == "before":
            selected.MoveEndpointByRange(END, selected, START)
        elif modes[mode] == "after":
            selected.MoveEndpointByRange(START, selected, END)
        selected.Select()
        return {
            "success": True,
            "action": "UIASelectText",
            "mode": mode,
            "state_invalidated": True,
        }

    @_operation
    def scroll(self, element: object, direction: str, pages: float) -> dict:
        self._guard(element)
        if direction not in ("up", "down", "left", "right"):
            raise ComputerUseError("Scroll direction must be up, down, left, or right.")
        if (
            isinstance(pages, bool)
            or not isinstance(pages, (int, float))
            or not math.isfinite(pages)
            or pages <= 0
        ):
            raise ComputerUseError("pages must be a positive finite number.")
        pattern = _pattern(element, "scroll")
        if pattern is None:
            raise ComputerUseError("The element does not support ScrollPattern.")
        axis = "Vertical" if direction in ("up", "down") else "Horizontal"
        if not getattr(pattern, "Current" + axis + "lyScrollable"):
            raise ComputerUseError("The requested axis is not scrollable.")
        view = float(getattr(pattern, "Current" + axis + "ViewSize"))
        position = float(getattr(pattern, "Current" + axis + "ScrollPercent"))
        if not (0 < view < 100 and 0 <= position <= 100):
            raise ComputerUseError(
                "The provider cannot express fractional page scrolling."
            )
        # UIA percent spans the scrollable extent, not the full document.
        delta = pages * 100 * view / (100 - view)
        target = min(
            100.0,
            max(0.0, position + (delta if direction in ("down", "right") else -delta)),
        )
        horizontal, vertical = (
            (NO_SCROLL, target) if axis == "Vertical" else (target, NO_SCROLL)
        )
        pattern.SetScrollPercent(horizontal, vertical)
        return {
            "success": True,
            "action": "UIAScroll",
            "direction": direction,
            "pages": pages,
            "scroll_percent": target,
            "state_invalidated": True,
        }
