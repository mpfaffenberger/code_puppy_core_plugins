"""Localization contracts owned by plugin implementations."""

import ast
from pathlib import Path

import code_puppy_core_plugins.claude_code_oauth.register_callbacks as callbacks


def test_no_raw_emit_in_claude_oauth_callbacks():
    """Fail if a display emitter bypasses the translation seam."""
    tree = ast.parse(Path(callbacks.__file__).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = (
            function.id
            if isinstance(function, ast.Name)
            else function.attr
            if isinstance(function, ast.Attribute)
            else ""
        )
        if not name.startswith("emit_"):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                if argument.value.strip():
                    offenders.append((node.lineno, name, argument.value[:50]))
            elif isinstance(argument, ast.JoinedStr):
                if any(
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and value.value.strip()
                    for value in argument.values
                ):
                    offenders.append((node.lineno, name, "f-string"))
    assert not offenders, f"Raw emit_* calls found: {offenders}"
