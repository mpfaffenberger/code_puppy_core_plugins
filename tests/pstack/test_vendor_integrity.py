"""The vendor tree is exactly what UPSTREAM.json pins, and fully classified."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

from code_puppy_core_plugins.pstack.catalog import (
    COMPANION_SKILLS,
    EXCLUDED_SKILLS,
    PRINCIPLE_PREFIX,
    SKILL_NAMESPACES,
    vendored_skill_names,
)
from code_puppy_core_plugins.pstack.upstream import (
    MANIFEST_PATH,
    PSTACK_DIR,
    TEAM_KIT,
    VENDOR_DIR,
    load_pin,
)

PINNED_COMMIT = "c86f349b102feb91f1966f8a6a60882a4e601b50"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _refresh_script() -> ModuleType:
    path = REPO_ROOT / "scripts" / "refresh_pstack_upstream.py"
    spec = importlib.util.spec_from_file_location("refresh_upstream", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _files_on_disk() -> set[str]:
    return {
        path.relative_to(VENDOR_DIR).as_posix()
        for path in VENDOR_DIR.rglob("*")
        if path.is_file() and path != MANIFEST_PATH and "__pycache__" not in path.parts
    }


def test_pin_matches_the_requested_upstream_commit() -> None:
    pin = load_pin()
    assert pin.commit == PINNED_COMMIT
    assert pin.repository == "https://github.com/cursor/plugins"
    plugin_json = json.loads((PSTACK_DIR / "plugin.json").read_text("utf-8"))
    assert pin.version == plugin_json["version"] == "0.15.23"


def test_every_vendored_file_matches_its_recorded_hash() -> None:
    recorded = load_pin().files
    assert _files_on_disk() == set(recorded)
    mismatched = [
        relative
        for relative, digest in recorded.items()
        if hashlib.sha256((VENDOR_DIR / relative).read_bytes()).hexdigest() != digest
    ]
    assert mismatched == []


def test_manifest_only_lists_paths_the_refresh_policy_keeps() -> None:
    is_vendored = _refresh_script().is_vendored
    assert all(is_vendored(relative) for relative in load_pin().files)
    assert not is_vendored("pstack/assets/logo.png")
    assert not is_vendored("pstack/docs/guide/img/01.png")
    assert is_vendored("pstack/docs/guide/01-start.md")
    assert is_vendored("cursor-team-kit/skills/deslop/SKILL.md")
    assert not is_vendored("cursor-team-kit/skills/fix-ci/SKILL.md")
    assert not is_vendored("ralph-loop/LICENSE")


def test_skill_inventory_matches_the_pin() -> None:
    names = vendored_skill_names()
    principles = [name for name in names if name.startswith(PRINCIPLE_PREFIX)]
    assert len(names) == 53
    assert len(principles) == 24
    assert {"poteto-mode", "how", "architect", "tdd", "interrogate"} <= set(names)
    assert sorted(p.name for p in (PSTACK_DIR / "agents").glob("*.md")) == [
        "comment-sicko.md",
        "poteto-agent.md",
    ]


def test_every_upstream_skill_is_classified_exactly_once() -> None:
    buckets: dict[str, list[str]] = {}
    for name in vendored_skill_names():
        if name in SKILL_NAMESPACES:
            buckets.setdefault(name, []).append("registered")
        if name in EXCLUDED_SKILLS:
            buckets.setdefault(name, []).append("excluded")
        if name.startswith(PRINCIPLE_PREFIX):
            buckets.setdefault(name, []).append("principle")
    unclassified = set(vendored_skill_names()) - set(buckets)
    doubled = {name: kinds for name, kinds in buckets.items() if len(kinds) > 1}
    assert unclassified == set()
    assert doubled == {}
    stale = (set(SKILL_NAMESPACES) | set(EXCLUDED_SKILLS)) - set(vendored_skill_names())
    assert stale == set()


def test_team_kit_companions_agree_everywhere() -> None:
    script_skills = set(_refresh_script().TEAM_KIT_SKILLS)
    assert script_skills == set(COMPANION_SKILLS)
    assert set(vendored_skill_names(TEAM_KIT)) == set(COMPANION_SKILLS)


def test_every_companion_is_invoked_by_pstack() -> None:
    corpus = "\n".join(
        path.read_text("utf-8") for path in (PSTACK_DIR / "skills").rglob("*.md")
    )
    assert [name for name in COMPANION_SKILLS if name not in corpus] == []


def test_license_and_attribution_ship_with_the_vendored_files() -> None:
    upstream_license = (PSTACK_DIR / "LICENSE").read_text("utf-8")
    assert upstream_license.startswith("MIT License")
    assert "Copyright (c) 2026 Lauren Tan" in upstream_license
    notices = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text("utf-8")
    assert PINNED_COMMIT in notices
    assert upstream_license.strip() in notices
    team_kit_license = (TEAM_KIT.root / "LICENSE").read_text("utf-8")
    assert "Copyright (c) 2026 Cursor" in team_kit_license
    assert team_kit_license.strip() in notices
    assert MANIFEST_PATH.is_file()
