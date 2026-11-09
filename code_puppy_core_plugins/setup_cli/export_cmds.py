"""``setup export|import`` — portable, secret-free configuration bundles.

Export copies an allowlist of user configuration (never token files,
trust stores, secrets, or session state) into a directory, optionally
in the Docker sbx config-mixin overlay layout. ``puppy.cfg`` is
sanitized: credential-looking keys are dropped. Import applies a
bundle back, backing up anything it overwrites.
"""

from __future__ import annotations

import configparser
import json
import shutil
import time
from pathlib import Path

from . import providers
from .output import SENSITIVE_MARKERS, Out, SetupError, confirm


# (source-path factory label, bundle-relative path)
def _file_map() -> list[tuple[Path, str]]:
    from code_puppy import config as cp

    entries: list[tuple[Path, str]] = [
        (Path(cp.CONFIG_DIR) / "onboarding_complete", "onboarding_complete"),
        (Path(cp.CONFIG_DIR) / "AGENTS.md", "AGENTS.md"),
        (Path(cp.MCP_SERVERS_FILE), "mcp_servers.json"),
        (Path(cp.CONFIG_DIR) / "mcp_agent_bindings.json", "mcp_agent_bindings.json"),
        (Path(cp.EXTRA_MODELS_FILE), "extra_models.json"),
        (Path(cp.CHATGPT_MODELS_FILE), "chatgpt_models.json"),
        (Path(cp.CLAUDE_MODELS_FILE), "claude_models.json"),
        (Path(cp.GEMINI_MODELS_FILE), "gemini_models.json"),
        (Path(cp.COPILOT_MODELS_FILE), "copilot_models.json"),
    ]
    return entries


def _dir_map() -> list[tuple[Path, str]]:
    from code_puppy import config as cp
    from code_puppy.plugins import USER_PLUGINS_DIR

    return [
        (Path(cp.AGENTS_DIR), "agents"),
        (Path(cp.SKILLS_DIR), "skills"),
        (Path(USER_PLUGINS_DIR), "plugins"),
    ]


EXCLUDED_NOTE = (
    "Excluded by design: OAuth token files (meta_oauth.json, *_oauth.json, "
    "copilot_*_tokens), secrets.json/keyring, trust stores "
    "(trusted_mcp.json, trusted_plugins.json), credential keys in "
    "puppy.cfg, sessions/autosaves/history, logs and caches."
)


def _is_credential_key(key: str, known_env_vars: set[str]) -> bool:
    if key.upper() in known_env_vars:
        return True
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_MARKERS)


def _sanitized_puppy_cfg(source: Path, dest: Path) -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(source, encoding="utf-8-sig")
    known = {name.upper() for name in providers.api_key_env_vars()}
    dropped = []
    for section in parser.sections() + ["DEFAULT"]:
        for key in list(parser[section].keys()):
            if _is_credential_key(key, known):
                parser.remove_option(section, key)
                dropped.append(key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as handle:
        parser.write(handle)
    return dropped


def cmd_export(out: Out, dest_dir: str, sbx_layout: bool) -> int:
    from code_puppy import config as cp

    dest = Path(dest_dir).expanduser()
    root = dest / "files/home/agent/.code_puppy" if sbx_layout else dest
    root.mkdir(parents=True, exist_ok=True)
    included: list[str] = []
    dropped_keys: list[str] = []

    cfg_source = Path(cp.CONFIG_FILE)
    if cfg_source.is_file():
        dropped_keys = _sanitized_puppy_cfg(cfg_source, root / "puppy.cfg")
        included.append("puppy.cfg (sanitized)")
    for source, rel in _file_map():
        if source.is_file():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            included.append(rel)
    for source, rel in _dir_map():
        if source.is_dir() and any(source.iterdir()):
            shutil.copytree(
                source,
                root / rel,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".state"),
            )
            included.append(f"{rel}/")

    creds_dest = None
    if sbx_layout:
        # The bundle must be runnable as-is: without this mixin sbx
        # never creates the credential env vars (e.g. META_API_KEY) in
        # the guest, and OAuth/API-key models silently never register.
        from . import sbx_plan

        creds_dest = sbx_plan.write_creds_mixin(dest / "code-puppy-creds")
        if creds_dest is not None:
            included.append("code-puppy-creds/ (credentials mixin)")

    manifest = {
        "kind": "code-puppy-setup-export",
        "sbx_layout": sbx_layout,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "included": included,
        "dropped_credential_keys": dropped_keys,
        "creds_mixin": str(creds_dest) if creds_dest is not None else None,
        "excluded_note": EXCLUDED_NOTE,
    }
    (dest / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    out.result(
        manifest,
        human=[
            f"exported to {dest}",
            *(f"  + {item}" for item in included),
            f"  ({EXCLUDED_NOTE})",
        ],
    )
    return 0


def _bundle_root(src: Path) -> Path:
    overlay = src / "files/home/agent/.code_puppy"
    return overlay if overlay.is_dir() else src


def cmd_import(out: Out, src_dir: str, assume_yes: bool, dry_run: bool) -> int:
    from code_puppy import config as cp

    src = _bundle_root(Path(src_dir).expanduser())
    if not src.is_dir():
        raise SetupError(f"bundle directory '{src_dir}' not found")

    plan: list[tuple[Path, Path, str]] = []  # source, dest, rel
    reverse_files = {rel: source for source, rel in _file_map()}
    for rel, dest in reverse_files.items():
        if (src / rel).is_file():
            plan.append((src / rel, dest, rel))
    if (src / "puppy.cfg").is_file():
        plan.append((src / "puppy.cfg", Path(cp.CONFIG_FILE), "puppy.cfg"))
    reverse_dirs = {rel: source for source, rel in _dir_map()}
    dir_plan = [
        (src / rel, dest, rel)
        for rel, dest in reverse_dirs.items()
        if (src / rel).is_dir()
    ]

    lines = [f"  {rel} -> {dest}" for _, dest, rel in plan] + [
        f"  {rel}/ -> {dest}/" for _, dest, rel in dir_plan
    ]
    if dry_run:
        out.result(
            {"would_import": [rel for _, _, rel in plan + dir_plan]},
            human=["dry run, would import:", *lines],
        )
        return 0
    if not lines:
        raise SetupError("nothing importable found in the bundle")
    if not out.json_mode:
        print("will import:")
        print("\n".join(lines))
    if not confirm(
        "Apply this bundle? Overwritten files get .bak copies.", assume_yes=assume_yes
    ):
        raise SetupError("aborted", exit_code=2)

    stamp = time.strftime("%Y%m%d%H%M%S")
    imported: list[str] = []
    for source, dest, rel in plan:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.copy2(dest, dest.with_suffix(dest.suffix + f".bak-{stamp}"))
        shutil.copy2(source, dest)
        imported.append(rel)
    for source, dest, rel in dir_plan:
        shutil.copytree(source, dest, dirs_exist_ok=True)
        imported.append(f"{rel}/")
    out.result(
        {"imported": imported},
        human=[
            f"imported: {', '.join(imported)}",
            "restart Code Puppy to pick up plugin/skill changes",
        ],
    )
    return 0
