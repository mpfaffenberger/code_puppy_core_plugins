"""Pattern-level tests independent of Windows and installed pywinauto."""

import sys
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from code_puppy_core_plugins.computer_use.backend_types import ComputerUseError
from code_puppy_core_plugins.computer_use.windows_accessibility import (
    WindowsAccessibility,
)


def wrapper(*, password=False, enabled=True, children=(), **patterns):
    raw = NS(
        CurrentIsPassword=password,
        CurrentIsEnabled=enabled,
        CurrentName="secret name",
        CurrentHelpText="secret help",
        CurrentIsKeyboardFocusable=True,
        CurrentHasKeyboardFocus=False,
        SetFocus=Mock(),
    )
    element = NS(
        element_info=NS(element=raw, control_type="Edit", runtime_id=None),
        children=children,
    )
    for name, pattern in patterns.items():
        setattr(element, "iface_" + name, pattern)
    return element


def value_pattern(**kwargs):
    return NS(
        CurrentValue="secret value", CurrentIsReadOnly=False, SetValue=Mock(), **kwargs
    )


@pytest.fixture
def adapter():
    return WindowsAccessibility()


def snapshot(adapter, root, limit=500):
    adapter._root = Mock(return_value=root)
    adapter._children = lambda element: iter(element.children)
    return adapter.snapshot(123, limit)


class TextRange:
    """Fake COM text range with UTF-16 endpoints and UIA character movement."""

    def __init__(self, document, start=0, end=None):
        self.document = document
        self.start = start
        self.end = len(document.data) // 2 if end is None else end

    def Clone(self):
        return TextRange(self.document, self.start, self.end)

    def GetText(self, count):
        assert count == -1
        return self.document.data[self.start * 2 : self.end * 2].decode("utf-16-le")

    def FindText(self, text, backward, ignore_case):
        assert backward is False and ignore_case is False
        encoded = text.encode("utf-16-le")
        for start in self.document.boundaries:
            if (
                self.start <= start
                and start + len(encoded) // 2 <= self.end
                and self.document.data[start * 2 : start * 2 + len(encoded)] == encoded
            ):
                return TextRange(self.document, start, start + len(encoded) // 2)
        return None

    def MoveEndpointByRange(self, endpoint, other, other_endpoint):
        value = other.start if other_endpoint == 0 else other.end
        if endpoint == 0:
            self.start = value
            self.end = max(self.end, value)
        else:
            self.end = value
            self.start = min(self.start, value)

    def MoveEndpointByUnit(self, endpoint, unit, count):
        assert (endpoint, unit, count) == (0, 0, 1)
        next_boundary = next(
            (value for value in self.document.boundaries if value > self.start), None
        )
        if next_boundary is None:
            return 0
        self.start = next_boundary
        self.end = max(self.end, self.start)
        return 1

    def CompareEndpoints(self, endpoint, other, other_endpoint):
        return (self.start if endpoint == 0 else self.end) - (
            other.start if other_endpoint == 0 else other.end
        )

    def Select(self):
        self.document.selection = (self.start, self.end)


class TextPattern:
    SupportedTextSelection = 1

    def __init__(self, text):
        self.data = text.encode("utf-16-le")
        self.boundaries = [0]
        for char in text:
            self.boundaries.append(
                self.boundaries[-1] + len(char.encode("utf-16-le")) // 2
            )
        self.selection = None
        self.DocumentRange = TextRange(self)


def test_snapshot_fields_and_password_descendants(adapter):
    secret_child = wrapper(value=value_pattern())
    password = wrapper(password=True, value=value_pattern(), children=[secret_child])
    ordinary = wrapper(value=value_pattern(), invoke=NS(Invoke=Mock()))
    root = wrapper(children=[password, ordinary])
    nodes, elements, metadata = snapshot(adapter, root)
    assert [node["id"] for node in nodes] == [1, 2, 3, 4]
    assert [node["parent_id"] for node in nodes] == [None, 1, 1, 2]
    assert elements[3] is ordinary
    for index in (1, 3):
        assert nodes[index]["title"] == nodes[index]["description"] == ""
        assert nodes[index]["value"] is None
        assert "UIASetValue" not in nodes[index]["actions"]
    assert nodes[2]["value"] == "secret value"
    assert "UIAInvoke" in nodes[2]["actions"]
    assert metadata["scanned_node_count"] == 4
    assert not metadata["truncated"]
    with pytest.raises(ComputerUseError, match="protected"):
        adapter.set_value(secret_child, "new")


def test_unknown_password_state_redacts_and_bounds(adapter):
    root = wrapper(value=value_pattern())
    del root.element_info.element.CurrentIsPassword
    root.element_info.rectangle = NS(left=1, top=2, right=11, bottom=22)
    nodes, _, _ = snapshot(adapter, root)
    assert nodes[0]["value"] is None
    assert nodes[0]["bounds"] == {"x": 1, "y": 2, "width": 10, "height": 20}


def test_bfs_limits_and_overflow(adapter):
    root = wrapper(children=[wrapper() for _ in range(2200)])
    nodes, elements, metadata = snapshot(adapter, root, 2)
    assert len(nodes) == len(elements) == 2
    assert metadata["scanned_node_count"] == 2000
    assert metadata["scan_limit_reached"] and metadata["truncated"]
    assert metadata["overflow_summary"] == {
        "excluded_node_count": 1998,
        "roles": {"Edit": 1998},
    }
    nodes, _, metadata = snapshot(adapter, wrapper(children=[wrapper()]), 1)
    assert metadata["truncated"] and not metadata["scan_limit_reached"]
    assert nodes[0]["id"] == 1


def test_cycle_is_bounded(adapter):
    root = wrapper()
    root.children = [root]
    nodes, _, metadata = snapshot(adapter, root)
    assert len(nodes) == 1
    assert metadata["scanned_node_count"] == 2


@pytest.mark.parametrize("limit", [0, 501, True, 1.5, "2", None])
def test_invalid_snapshot_limits(adapter, limit):
    with pytest.raises(ComputerUseError):
        adapter.snapshot(123, limit)


@pytest.mark.parametrize("hwnd", [0, -1, False, "123"])
def test_invalid_hwnd(adapter, hwnd):
    with pytest.raises(ComputerUseError):
        adapter.snapshot(hwnd, 1)


def test_lazy_desktop_uia_backend(adapter, monkeypatch):
    root = wrapper()
    desktop = Mock()
    desktop.return_value.window.return_value.wrapper_object.return_value = root
    monkeypatch.setitem(sys.modules, "pywinauto", NS(Desktop=desktop))
    adapter._children = lambda element: iter(())
    adapter.snapshot(456, 1)
    desktop.assert_called_once_with(backend="uia")
    desktop.return_value.window.assert_called_once_with(handle=456)


@pytest.mark.parametrize(
    "pattern,action,method",
    [
        ("invoke", "UIAInvoke", "Invoke"),
        ("selection_item", "UIASelect", "Select"),
        ("toggle", "UIAToggle", "Toggle"),
        ("expand_collapse", "UIAExpand", "Expand"),
        ("expand_collapse", "UIACollapse", "Collapse"),
    ],
)
def test_actual_action_pattern(adapter, pattern, action, method):
    call = Mock()
    interface = NS(CurrentExpandCollapseState=0, **{method: call})
    result = adapter.perform_action(wrapper(**{pattern: interface}), action.lower())
    call.assert_called_once_with()
    assert result["action"] == action


def test_focus_uses_raw_com_not_wrapper_input(adapter):
    element = wrapper()
    adapter.perform_action(element, "UIASetFocus")
    element.element_info.element.SetFocus.assert_called_once_with()


@pytest.mark.parametrize(
    "patterns,expected",
    [
        (("invoke", "selection_item", "toggle"), "UIAInvoke"),
        (("selection_item", "toggle"), "UIASelect"),
        (("toggle",), "UIAToggle"),
    ],
)
def test_click_pattern_priority(adapter, patterns, expected):
    interfaces = {
        name: NS(Invoke=Mock(), Select=Mock(), Toggle=Mock()) for name in patterns
    }
    assert adapter.click(wrapper(**interfaces))["action"] == expected


def test_unsupported_disabled_and_provider_errors(adapter):
    with pytest.raises(ComputerUseError, match="No semantic"):
        adapter.click(wrapper())
    with pytest.raises(ComputerUseError, match="disabled"):
        adapter.click(wrapper(enabled=False, invoke=NS(Invoke=Mock())))
    with pytest.raises(ComputerUseError, match="not supported"):
        adapter.perform_action(wrapper(), "UIAToggle")
    with pytest.raises(ComputerUseError, match="refresh") as error:
        adapter.click(
            wrapper(invoke=NS(Invoke=Mock(side_effect=RuntimeError("secret"))))
        )
    assert "secret" not in str(error.value)


def test_parameterized_actions_require_specific_method(adapter):
    with pytest.raises(ComputerUseError, match="corresponding"):
        adapter.perform_action(wrapper(value=value_pattern()), "UIASetValue")


def test_value_and_range_value_selection(adapter):
    value, numeric = value_pattern(), value_pattern(CurrentMinimum=0, CurrentMaximum=10)
    adapter.set_value(wrapper(value=value, range_value=numeric), "literal")
    value.SetValue.assert_called_once_with("literal")
    numeric.SetValue.assert_not_called()
    adapter.set_value(wrapper(range_value=numeric), "2.5")
    numeric.SetValue.assert_called_once_with(2.5)


@pytest.mark.parametrize("text", ["nan", "inf", "abc", "-1", "11"])
def test_invalid_range_value(adapter, text):
    numeric = value_pattern(CurrentMinimum=0, CurrentMaximum=10)
    with pytest.raises(ComputerUseError):
        adapter.set_value(wrapper(range_value=numeric), text)
    numeric.SetValue.assert_not_called()


def test_read_only_password_and_missing_value(adapter):
    pattern = value_pattern()
    pattern.CurrentIsReadOnly = True
    for element in [
        wrapper(value=pattern),
        wrapper(password=True, value=value_pattern()),
        wrapper(),
    ]:
        with pytest.raises(ComputerUseError):
            adapter.set_value(element, "new")
    with pytest.raises(ComputerUseError):
        adapter.set_value(wrapper(value=value_pattern()), 3)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("text", (3, 9)),
        ("before", (3, 3)),
        ("after", (9, 9)),
        ("cursor_before", (3, 3)),
        ("cursor_after", (9, 9)),
    ],
)
def test_text_range_utf16_and_cursor_endpoints(adapter, mode, expected):
    text = TextPattern("\U0001f680 target end")
    result = adapter.select_text(wrapper(text=text), "target", mode)
    assert text.selection == expected
    assert result["success"]


def test_exact_context_and_ambiguity(adapter):
    text = TextPattern("first target! second target? TARGET")
    element = wrapper(text=text)
    with pytest.raises(ComputerUseError, match="ambiguous"):
        adapter.select_text(element, "target")
    assert text.selection is None
    adapter.select_text(element, "target", prefix="second ", suffix="?")
    assert text.selection == (21, 27)
    with pytest.raises(ComputerUseError, match="no exact"):
        adapter.select_text(element, "target", suffix="missing")
    with pytest.raises(ComputerUseError, match="ambiguous"):
        adapter.select_text(wrapper(text=TextPattern("aaa")), "aa")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": ""},
        {"text": "a", "mode": "bad"},
        {"text": "a", "prefix": 1},
        {"text": None},
    ],
)
def test_invalid_selection_arguments(adapter, kwargs):
    with pytest.raises(ComputerUseError):
        adapter.select_text(wrapper(text=TextPattern("a")), **kwargs)


def test_unselectable_and_password_text(adapter):
    pattern = TextPattern("secret")
    pattern.SupportedTextSelection = 0
    for element in (
        wrapper(text=pattern),
        wrapper(),
        wrapper(password=True, text=TextPattern("secret")),
    ):
        with pytest.raises(ComputerUseError):
            adapter.select_text(element, "secret")


def scroll_pattern(**overrides):
    values = {
        "CurrentHorizontallyScrollable": True,
        "CurrentVerticallyScrollable": True,
        "CurrentHorizontalViewSize": 20,
        "CurrentVerticalViewSize": 20,
        "CurrentHorizontalScrollPercent": 50,
        "CurrentVerticalScrollPercent": 50,
        "SetScrollPercent": Mock(),
    }
    values.update(overrides)
    return NS(**values)


@pytest.mark.parametrize(
    "direction,pages,expected",
    [
        ("down", 0.5, (-1, 62.5)),
        ("up", 0.5, (-1, 37.5)),
        ("right", 1, (75, -1)),
        ("left", 1, (25, -1)),
        ("down", 100, (-1, 100)),
        ("up", 100, (-1, 0)),
    ],
)
def test_fractional_scroll_pages(adapter, direction, pages, expected):
    pattern = scroll_pattern()
    assert adapter.scroll(wrapper(scroll=pattern), direction, pages)["success"]
    pattern.SetScrollPercent.assert_called_once_with(*expected)


@pytest.mark.parametrize("pages", [0, -1, True, "1", float("nan"), float("inf")])
def test_invalid_scroll_pages(adapter, pages):
    with pytest.raises(ComputerUseError):
        adapter.scroll(wrapper(scroll=scroll_pattern()), "down", pages)


def test_scroll_unsupported_axis_and_view(adapter):
    for element in (
        wrapper(),
        wrapper(scroll=scroll_pattern(CurrentVerticallyScrollable=False)),
        wrapper(scroll=scroll_pattern(CurrentVerticalViewSize=100)),
        wrapper(scroll=scroll_pattern(CurrentVerticalViewSize=0)),
    ):
        with pytest.raises(ComputerUseError):
            adapter.scroll(element, "down", 0.5)
    with pytest.raises(ComputerUseError):
        adapter.scroll(wrapper(scroll=scroll_pattern()), "diagonal", 1)


def test_advertised_actions_are_supported(adapter):
    value = value_pattern()
    value.CurrentIsReadOnly = True
    root = wrapper(
        value=value,
        text=TextPattern("abc"),
        expand_collapse=NS(CurrentExpandCollapseState=3),
        scroll=scroll_pattern(
            CurrentHorizontallyScrollable=False, CurrentVerticallyScrollable=False
        ),
    )
    nodes, _, _ = snapshot(adapter, root)
    assert nodes[0]["actions"] == ["UIASetFocus", "UIASelectText"]
    root.element_info.element.CurrentIsEnabled = False
    assert snapshot(adapter, root)[0][0]["actions"] == []


def test_thread_affinity(adapter):
    snapshot(adapter, wrapper())
    errors = []

    def wrong_thread():
        try:
            adapter.click(wrapper())
        except ComputerUseError as error:
            errors.append(str(error))

    worker = threading.Thread(target=wrong_thread)
    worker.start()
    worker.join()
    assert len(errors) == 1 and "COM worker thread" in errors[0]


def test_text_search_budget_and_nonadvancing_provider(adapter, monkeypatch):
    from code_puppy_core_plugins.computer_use import windows_accessibility as module

    monkeypatch.setattr(module, "MAX_TEXT_MATCHES", 1)
    with pytest.raises(ComputerUseError, match="limit"):
        adapter.select_text(wrapper(text=TextPattern("a b")), "a")
    monkeypatch.setattr(TextRange, "MoveEndpointByUnit", lambda *args: 0)
    with pytest.raises(ComputerUseError, match="advance"):
        adapter.select_text(wrapper(text=TextPattern("a b")), "a")


def test_missing_lazy_dependency(adapter, monkeypatch):
    monkeypatch.setitem(sys.modules, "pywinauto", None)
    with pytest.raises(ComputerUseError, match="requires pywinauto"):
        adapter.snapshot(123, 1)


def test_real_child_walker_contract_is_lazy(adapter, monkeypatch):
    first, second = object(), object()
    walker = NS(
        GetFirstChildElement=Mock(return_value=first),
        GetNextSiblingElement=Mock(side_effect=[second, None]),
    )
    factory = Mock(side_effect=lambda info: info)
    monkeypatch.setitem(
        sys.modules,
        "pywinauto.uia_defines",
        NS(IUIA=lambda: NS(iuia=NS(ControlViewWalker=walker))),
    )
    monkeypatch.setitem(
        sys.modules,
        "pywinauto.uia_element_info",
        NS(UIAElementInfo=lambda element: element),
    )
    monkeypatch.setitem(
        sys.modules, "pywinauto.controls.uiawrapper", NS(UIAWrapper=factory)
    )
    root = wrapper()
    children = adapter._children(root)
    walker.GetFirstChildElement.assert_not_called()
    assert next(children) is first
    walker.GetFirstChildElement.assert_called_once_with(root.element_info.element)
    walker.GetNextSiblingElement.assert_not_called()
    assert list(children) == [second]
    assert factory.call_count == 2


def test_text_at_document_end_and_range_boundary_values(adapter):
    pattern = TextPattern("a")
    adapter.select_text(wrapper(text=pattern), "a", "after")
    assert pattern.selection == (1, 1)
    numeric = value_pattern(CurrentMinimum=0, CurrentMaximum=10)
    adapter.set_value(wrapper(range_value=numeric), "0")
    adapter.set_value(wrapper(range_value=numeric), "10")
    assert [call.args for call in numeric.SetValue.call_args_list] == [(0.0,), (10.0,)]
