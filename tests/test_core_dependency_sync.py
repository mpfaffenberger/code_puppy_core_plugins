"""Guard: the test venv must satisfy Code Puppy core's declared dependencies.

Core is installed outside ``uv.lock`` (editable per the README, from git in
CI), so a stale lock lets ``uv run`` silently downgrade packages that core
pins. The symptom is dozens of unrelated ImportErrors and AttributeErrors;
this test names the real cause instead.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, requires, version

import pytest
from packaging.requirements import Requirement

# Our own version is bumped at publish time and never committed back, so a
# checkout always "violates" core's floor on it. That is not real drift.
_IGNORED = {"code-puppy-core-plugins"}

_FIX_HINT = (
    "Fix: `uv pip install -e ../code_puppy`. If `uv run` undoes it, the lock "
    "is stale: `uv lock --upgrade-package <name>==<core's version>`."
)


def _core_runtime_requirements() -> list[Requirement]:
    try:
        raw = requires("code-puppy") or []
    except PackageNotFoundError:
        pytest.skip("code-puppy core is not installed")
    parsed = (Requirement(line) for line in raw)
    # Evaluating with no extra drops optional-dependency groups.
    return [
        req
        for req in parsed
        if req.name not in _IGNORED
        and (req.marker is None or req.marker.evaluate({"extra": ""}))
    ]


def test_venv_satisfies_core_dependencies() -> None:
    problems = []
    for req in _core_runtime_requirements():
        try:
            installed = version(req.name)
        except PackageNotFoundError:
            problems.append(f"{req.name}: not installed (core requires {req})")
            continue
        if not req.specifier.contains(installed, prereleases=True):
            problems.append(
                f"{req.name}: {installed} installed, core requires {req.specifier}"
            )

    assert not problems, (
        "Test venv has drifted from Code Puppy core's dependencies:\n  "
        + "\n  ".join(problems)
        + "\n"
        + _FIX_HINT
    )
