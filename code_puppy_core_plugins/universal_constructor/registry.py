"""UC Tool Registry - discovers and manages user-created tools.

This module provides the core registry that scans the user's UC directory,
loads tool metadata, extracts function signatures, and provides access
to enabled tools for the LLM.
"""

import ast
import importlib.util
import logging
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Callable, Dict, List, Optional

from . import USER_UC_DIR
from .models import ToolMeta, UCToolInfo

logger = logging.getLogger(__name__)


class UCRegistry:
    """Registry for discovering and managing UC tools.

    Scans the user's UC directory recursively, loading tool metadata
    and providing access to enabled tools. Supports namespacing via
    subdirectories (e.g., api/weather.py → "api.weather").
    """

    def __init__(self, tools_dir: Optional[Path] = None):
        """Initialize the registry.

        Args:
            tools_dir: Directory to scan for tools. Defaults to USER_UC_DIR.
        """
        self._tools_dir = tools_dir or USER_UC_DIR
        self._tools: Dict[str, UCToolInfo] = {}
        self._modules: Dict[str, ModuleType] = {}
        self._last_scan: Optional[datetime] = None

    def ensure_tools_dir(self) -> Path:
        """Ensure the tools directory exists.

        Returns:
            Path to the tools directory.
        """
        self._tools_dir.mkdir(parents=True, exist_ok=True)
        return self._tools_dir

    def scan(self) -> int:
        """Scan the tools directory and load all tools.

        Returns:
            Number of tools found.
        """
        self._tools.clear()
        self._modules.clear()

        if not self._tools_dir.exists():
            logger.debug(f"Tools directory does not exist: {self._tools_dir}")
            return 0

        # Find all Python files recursively
        tool_files = list(self._tools_dir.rglob("*.py"))

        for tool_file in tool_files:
            # Skip __init__.py and hidden files
            if tool_file.name.startswith("_") or tool_file.name.startswith("."):
                continue

            try:
                tool_info = self._load_tool_file(tool_file)
                if tool_info:
                    self._tools[tool_info.full_name] = tool_info
                    logger.debug(f"Loaded tool: {tool_info.full_name}")
            except Exception as e:
                logger.warning(f"Failed to load tool from {tool_file}: {e}")

        self._last_scan = datetime.now()
        logger.info(f"Scanned {len(self._tools)} tools from {self._tools_dir}")
        return len(self._tools)

    def _load_tool_file(self, file_path: Path) -> Optional[UCToolInfo]:
        """Load a tool from a Python file.

        Args:
            file_path: Path to the Python file.

        Returns:
            UCToolInfo if valid tool, None otherwise.
        """
        # Calculate namespace from relative path
        try:
            rel_path = file_path.relative_to(self._tools_dir)
            namespace_parts = list(rel_path.parent.parts)
            namespace = ".".join(namespace_parts) if namespace_parts else ""
        except ValueError:
            namespace = ""

        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(file_path))
        except (OSError, UnicodeError, SyntaxError) as e:
            logger.warning("Failed to parse tool source %s: %s", file_path, e)
            return None

        # Metadata is deliberately restricted to a top-level literal. Reading
        # it must not execute generated code or evaluate arbitrary expressions.
        meta_node: ast.Assign | None = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "TOOL_META"
                for target in node.targets
            ):
                meta_node = node

        if meta_node is None:
            logger.debug("No top-level TOOL_META found in %s", file_path)
            return None

        try:
            raw_meta = ast.literal_eval(meta_node.value)
        except (ValueError, TypeError, SyntaxError) as e:
            logger.warning("Invalid literal TOOL_META in %s: %s", file_path, e)
            return None

        if not isinstance(raw_meta, dict):
            logger.warning("TOOL_META is not a dict in %s", file_path)
            return None

        raw_meta = dict(raw_meta)

        # Set namespace from directory structure
        raw_meta["namespace"] = namespace

        # Parse metadata
        try:
            meta = ToolMeta(**raw_meta)
        except Exception as e:
            logger.warning(f"Invalid TOOL_META in {file_path}: {e}")
            return None

        function_nodes = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        func_name = next(
            (
                candidate
                for candidate in (meta.name, "run", "execute")
                if candidate in function_nodes
            ),
            None,
        )
        if func_name is None:
            public_names = sorted(
                name for name in function_nodes if not name.startswith("_")
            )
            func_name = public_names[0] if public_names else None

        if func_name is None:
            logger.warning("No top-level function found in %s", file_path)
            return None

        function_node = function_nodes[func_name]
        try:
            signature_str = f"{func_name}({ast.unparse(function_node.args)})"
            if function_node.returns is not None:
                signature_str += f" -> {ast.unparse(function_node.returns)}"
        except (AttributeError, ValueError):
            signature_str = f"{func_name}(...)"

        docstring = ast.get_docstring(function_node, clean=True)

        return UCToolInfo(
            meta=meta,
            signature=signature_str,
            source_path=str(file_path),
            function_name=func_name,
            docstring=docstring,
        )

    def _load_module(self, file_path: Path) -> Optional[ModuleType]:
        """Load a Python module from a file path.

        Args:
            file_path: Path to the Python file.

        Returns:
            Loaded module or None if failed.
        """
        original_path = sys.path
        baseline_path = list(original_path)
        module: ModuleType | None = None
        module_name: str | None = None

        try:
            source_hash = hash(file_path.read_bytes())
            module_name = (
                f"uc_tool_{file_path.stem}_{hash((str(file_path), source_hash))}"
            )
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec is None or spec.loader is None:
                logger.warning("Could not create import spec for UC tool %s", file_path)
                return None

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            source = file_path.read_text(encoding="utf-8")
            code = compile(source, str(file_path), "exec")
            exec(code, module.__dict__)

            additions = []
            for entry in list(sys.path):
                if entry not in baseline_path and entry not in additions:
                    additions.append(entry)
            original_path[:] = baseline_path + additions
            sys.path = original_path
            return module

        except BaseException as e:
            original_path[:] = baseline_path
            sys.path = original_path
            if module_name is not None:
                sys.modules.pop(module_name, None)
            logger.warning("Failed to execute UC tool module %s: %s", file_path, e)
            return None

    def list_tools(self, include_disabled: bool = False) -> List[UCToolInfo]:
        """List all discovered tools.

        Args:
            include_disabled: Whether to include disabled tools.

        Returns:
            List of tool info objects.
        """
        if not self._tools:
            self.scan()

        tools = list(self._tools.values())
        if not include_disabled:
            tools = [t for t in tools if t.meta.enabled]

        return sorted(tools, key=lambda t: t.full_name)

    def get_tool(self, name: str) -> Optional[UCToolInfo]:
        """Get a specific tool by name.

        Args:
            name: Full tool name (including namespace).

        Returns:
            Tool info or None if not found.
        """
        if not self._tools:
            self.scan()

        return self._tools.get(name)

    def get_tool_function(self, name: str) -> Optional[Callable]:
        """Get the callable function for a tool.

        Args:
            name: Full tool name (including namespace).

        Returns:
            Callable function or None if not found.
        """
        tool = self.get_tool(name)
        if tool is None:
            return None

        module = self.load_tool_module(name)
        if module is None:
            return None

        func = getattr(module, tool.function_name, None)
        if func is None or not callable(func) or isinstance(func, type):
            logger.warning(
                "Selected function %s is not callable in UC tool %s",
                tool.function_name,
                tool.source_path,
            )
            return None
        return func

    def load_tool_module(self, name: str) -> Optional[ModuleType]:
        """Get the loaded module for a tool.

        Args:
            name: Full tool name (including namespace).

        Returns:
            Module or None if not found.
        """
        tool = self.get_tool(name)
        if tool is None:
            return None

        module = self._modules.get(name)
        if module is None:
            module = self._load_module(Path(tool.source_path))
            if module is None:
                return None
            self._modules[name] = module
        return module

    def reload(self) -> int:
        """Force a rescan of all tools.

        Returns:
            Number of tools found.
        """
        return self.scan()


# Global registry instance
_registry: Optional[UCRegistry] = None


def get_registry() -> UCRegistry:
    """Get the global UC registry instance.

    Returns:
        The global UCRegistry instance.
    """
    global _registry
    if _registry is None:
        _registry = UCRegistry()
    return _registry
