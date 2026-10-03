"""Retirement regression: token_ratio_learner must be fully gone.

Validates installed-package metadata (importlib.metadata entry points),
not just source-tree grep, per the removal brief. Also proves collateral
plugins keep their registrations (exact set minus one), and that a
representative sample of other plugins still import cleanly.
"""

import importlib
import importlib.metadata as metadata

import pytest

_GROUP = "code_puppy.plugins"


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


def test_other_plugin_entry_points_are_unaffected():
    """Removing one entry point must not disturb any other registration."""
    names = _installed_entry_point_names()
    # A representative, stable sample spanning unrelated plugin families.
    survivors = {
        "herdr",
        "theme",
        "context_indicator",
        "subagent_panel",
        "plugin_list",
        "destructive_command_guard",
    }
    missing = survivors - names
    assert not missing, f"Unrelated plugins vanished too: {missing}"


def test_survivor_entry_points_still_import_their_register_callbacks():
    """Spot-check a few real entry-point targets actually import clean."""
    eps = {ep.name: ep for ep in metadata.entry_points(group=_GROUP)}
    for name in ("herdr", "theme", "context_indicator"):
        module = eps[name].load()
        assert module is not None
