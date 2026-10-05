"""Retirement regression: token_ratio_learner must be fully gone.

Validates installed-package metadata (importlib.metadata entry points),
not just source-tree grep. A representative sample of surviving plugins
must also import cleanly, not merely remain registered. Unrelated registry
changes do not require updating this retirement regression.
"""

import importlib
import importlib.metadata as metadata

import pytest

_GROUP = "code_puppy.plugins"

# A sample spanning unrelated plugin families, actually loaded (not just
# checked for registration) to back the PR's "load cleanly" claim.
_LOAD_CHECK_SAMPLE = (
    "herdr",
    "theme",
    "context_indicator",
    "subagent_panel",
    "plugin_list",
    "destructive_command_guard",
)


def _installed_entry_point_names() -> set[str]:
    return {ep.name for ep in metadata.entry_points(group=_GROUP)}


def test_token_ratio_learner_entry_point_is_gone():
    names = _installed_entry_point_names()
    assert "token_ratio_learner" not in names, (
        "token_ratio_learner must not be an installed code_puppy.plugins "
        "entry point after retirement"
    )


def test_token_ratio_learner_package_is_uninstalled():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("code_puppy_core_plugins.token_ratio_learner")


def test_survivor_entry_points_actually_load():
    """Actually import() each sampled entry point target, not just check
    that its name is still registered."""
    eps = {ep.name: ep for ep in metadata.entry_points(group=_GROUP)}
    for name in _LOAD_CHECK_SAMPLE:
        module = eps[name].load()
        assert module is not None
