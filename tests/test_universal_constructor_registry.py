"""Security and lazy-loading tests for the Universal Constructor registry."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from code_puppy_core_plugins.universal_constructor.registry import UCRegistry


def _write_tool(path: Path, body: str, *, name: str = "tool") -> None:
    path.write_text(
        f'TOOL_META = {{"name": "{name}", "description": "test"}}\n\n{body}',
        encoding="utf-8",
    )


def test_scan_does_not_execute_generated_module(tmp_path):
    marker = tmp_path / "marker.txt"
    poison_path = tmp_path / "poison"
    tool = tmp_path / "poison.py"
    _write_tool(
        tool,
        f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(poison_path)!r})
Path({str(marker)!r}).write_text("executed")
raise RuntimeError("must not run during scan")

def tool():
    return 1
""",
    )
    before = list(sys.path)
    registry = UCRegistry(tools_dir=tmp_path)

    try:
        assert registry.scan() == 1
        assert registry.get_tool("tool") is not None
        assert not marker.exists()
        assert sys.path == before
        assert registry._modules == {}
    finally:
        sys.path[:] = before


def test_disabled_tool_listing_does_not_execute_generated_module(tmp_path):
    marker = tmp_path / "marker.txt"
    tool = tmp_path / "disabled.py"
    tool.write_text(
        f"""
TOOL_META = {{"name": "disabled", "description": "test", "enabled": False}}
from pathlib import Path
Path({str(marker)!r}).write_text("executed")
raise RuntimeError("must not run during listing")

def disabled():
    return 1
""",
        encoding="utf-8",
    )
    registry = UCRegistry(tools_dir=tmp_path)

    assert registry.scan() == 1
    assert [tool.full_name for tool in registry.list_tools()] == []
    assert [tool.full_name for tool in registry.list_tools(include_disabled=True)] == [
        "disabled"
    ]
    assert not marker.exists()


def test_scan_never_calls_load_module(tmp_path):
    _write_tool(tmp_path / "tool.py", "def tool():\n    return 1\n")
    registry = UCRegistry(tools_dir=tmp_path)

    with patch.object(registry, "_load_module", side_effect=AssertionError):
        assert registry.scan() == 1


def test_scan_rejects_computed_metadata_without_execution(tmp_path):
    marker = tmp_path / "marker.txt"
    (tmp_path / "computed.py").write_text(
        f"""
from pathlib import Path

def build_meta():
    Path({str(marker)!r}).write_text("executed")
    return {{"name": "computed", "description": "test"}}

TOOL_META = build_meta()

def computed():
    return 1
""",
        encoding="utf-8",
    )
    registry = UCRegistry(tools_dir=tmp_path)

    assert registry.scan() == 0
    assert not marker.exists()


def test_last_top_level_metadata_assignment_wins(tmp_path):
    tool = tmp_path / "tool.py"
    tool.write_text(
        """
TOOL_META = {"name": "first", "description": "first"}
TOOL_META = {"name": "second", "description": "second"}

def second():
    return 2
""",
        encoding="utf-8",
    )
    registry = UCRegistry(tools_dir=tmp_path)

    assert registry.scan() == 1
    assert registry.get_tool("second").meta.description == "second"


@pytest.mark.parametrize(
    "source",
    [
        "def make():\n    TOOL_META = {'name': 'nested', 'description': 'test'}",
        "if True:\n    TOOL_META = {'name': 'conditional', 'description': 'test'}",
    ],
)
def test_nested_and_conditional_metadata_is_rejected(tmp_path, source):
    (tmp_path / "invalid.py").write_text(source, encoding="utf-8")
    assert UCRegistry(tools_dir=tmp_path).scan() == 0


def test_syntax_error_is_ignored_during_scan(tmp_path):
    (tmp_path / "broken.py").write_text("TOOL_META = {", encoding="utf-8")

    assert UCRegistry(tools_dir=tmp_path).scan() == 0


def test_read_error_is_ignored_during_scan(tmp_path):
    tool = tmp_path / "unreadable.py"
    _write_tool(tool, "def tool():\n    return 1\n")
    registry = UCRegistry(tools_dir=tmp_path)

    with patch.object(Path, "read_text", side_effect=OSError("denied")):
        assert registry.scan() == 0


def test_static_signature_and_docstring(tmp_path):
    _write_tool(
        tmp_path / "work.py",
        '''
def work(
    a: int = 1,
    /,
    b: str = "x",
    *args: float,
    flag: bool = True,
    **kwargs: bytes,
) -> dict[str, int]:
    """A documented tool."""
    return {"a": a}
''',
        name="work",
    )
    registry = UCRegistry(tools_dir=tmp_path)

    assert registry.scan() == 1
    tool = registry.get_tool("work")
    assert tool.signature == (
        "work(a: int=1, /, b: str='x', *args: float, flag: bool=True, "
        "**kwargs: bytes) -> dict[str, int]"
    )
    assert tool.docstring == "A documented tool."


@pytest.mark.parametrize(
    ("name", "functions", "expected"),
    [
        (
            "target",
            "def target(): pass\ndef run(): pass\ndef execute(): pass",
            "target",
        ),
        ("missing", "def run(): pass\ndef execute(): pass", "run"),
        ("missing", "async def execute(): pass\ndef zebra(): pass", "execute"),
        ("missing", "def execute(): pass\ndef zebra(): pass", "execute"),
        ("missing", "def zebra(): pass\ndef alpha(): pass", "alpha"),
    ],
)
def test_function_selection_priority(tmp_path, name, functions, expected):
    _write_tool(tmp_path / "tool.py", functions, name=name)
    registry = UCRegistry(tools_dir=tmp_path)

    assert registry.scan() == 1
    assert registry.get_tool(next(iter(registry._tools))).function_name == expected


def test_non_top_level_callables_are_not_entry_points(tmp_path):
    (tmp_path / "invalid.py").write_text(
        """
from math import sqrt
TOOL_META = {"name": "invalid", "description": "test"}
lambda_tool = lambda: 1

class Tool:
    def method(self):
        return 1
""",
        encoding="utf-8",
    )

    assert UCRegistry(tools_dir=tmp_path).scan() == 0


def test_load_tool_module_executes_once_and_caches(tmp_path):
    marker = tmp_path / "runs.txt"
    _write_tool(
        tmp_path / "tool.py",
        f"""
from pathlib import Path
with Path({str(marker)!r}).open("a", encoding="utf-8") as stream:
    stream.write("run\\n")

def tool():
    return 1
""",
    )
    registry = UCRegistry(tools_dir=tmp_path)
    assert registry.scan() == 1
    assert not marker.exists()

    first = registry.load_tool_module("tool")
    second = registry.load_tool_module("tool")

    assert first is second
    assert marker.read_text(encoding="utf-8").splitlines() == ["run"]


def test_get_tool_function_loads_lazily(tmp_path):
    marker = tmp_path / "marker.txt"
    _write_tool(
        tmp_path / "tool.py",
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("loaded")

def tool(value: int) -> int:
    return value + 1
""",
    )
    registry = UCRegistry(tools_dir=tmp_path)
    assert registry.scan() == 1
    assert not marker.exists()

    function = registry.get_tool_function("tool")

    assert marker.read_text(encoding="utf-8") == "loaded"
    assert function(1) == 2


def test_successful_load_preserves_original_sys_path_precedence(tmp_path):
    dependency_dir = tmp_path / "dependency"
    dependency_dir.mkdir()
    (dependency_dir / "helper_dep.py").write_text("VALUE = 42\n", encoding="utf-8")
    tool = tmp_path / "tool.py"
    _write_tool(
        tool,
        f"""
import sys
sys.path.insert(0, {str(dependency_dir)!r})

def tool():
    from helper_dep import VALUE
    return VALUE
""",
    )
    registry = UCRegistry(tools_dir=tmp_path)
    before = list(sys.path)
    sys.modules.pop("helper_dep", None)

    try:
        assert registry.scan() == 1
        function = registry.get_tool_function("tool")
        assert sys.path[: len(before)] == before
        assert sys.path[-1] == str(dependency_dir)
        assert function() == 42
    finally:
        sys.path[:] = before
        sys.modules.pop("helper_dep", None)


def test_failed_load_restores_path_and_cleans_partial_module(tmp_path, caplog):
    poison_path = tmp_path / "poison"
    tool = tmp_path / "broken.py"
    _write_tool(
        tool,
        f"""
import sys
sys.path.insert(0, {str(poison_path)!r})
raise RuntimeError("boom")

def broken():
    return 1
""",
        name="broken",
    )
    registry = UCRegistry(tools_dir=tmp_path)
    before = list(sys.path)

    try:
        assert registry.scan() == 1
        with caplog.at_level("WARNING"):
            assert registry.load_tool_module("broken") is None
        assert sys.path == before
        assert not any(
            getattr(module, "__file__", None) == str(tool)
            for module in sys.modules.values()
            if module is not None
        )
        assert str(tool) in caplog.text
        assert "boom" in caplog.text
    finally:
        sys.path[:] = before


def test_reload_clears_cache_without_executing_updated_source(tmp_path):
    marker = tmp_path / "runs.txt"
    tool = tmp_path / "tool.py"
    _write_tool(
        tool,
        f"""
from pathlib import Path
with Path({str(marker)!r}).open("a", encoding="utf-8") as stream:
    stream.write("one\\n")

def tool():
    return 1
""",
    )
    registry = UCRegistry(tools_dir=tmp_path)
    assert registry.scan() == 1
    first = registry.load_tool_module("tool")
    assert marker.read_text(encoding="utf-8").splitlines() == ["one"]

    _write_tool(
        tool,
        f"""
from pathlib import Path
with Path({str(marker)!r}).open("a", encoding="utf-8") as stream:
    stream.write("two\\n")

def tool():
    return 2
""",
    )
    assert registry.reload() == 1
    assert marker.read_text(encoding="utf-8").splitlines() == ["one"]

    second_module = registry.load_tool_module("tool")
    second = registry.get_tool_function("tool")
    assert first is not second_module
    assert second() == 2
    assert marker.read_text(encoding="utf-8").splitlines() == ["one", "two"]
