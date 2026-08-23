"""Content-hash trust store for project-level Claude Code hook configs.

Project ``.claude/settings.json`` files can declare hooks that run arbitrary
shell commands at lifecycle events. ``SessionStart`` fires during boot, so a
hostile repo is a workstation-compromise vector the moment the user runs
``cd <repo> && code-puppy``. The project ``hooks`` subtree is therefore
disabled until explicitly accepted via ``/hooks trust``.

The store lives user-side so a repo can never self-trust. Array order
survives canonicalization because hook execution order is semantic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

TRUST_STORE_FILE = Path.home() / ".code_puppy" / "trusted_hooks.json"
PROJECT_SETTINGS_RELPATH = Path(".claude") / "settings.json"

_STORE_VERSION = 1

TRUSTED = "trusted"
CHANGED = "changed"
UNTRUSTED = "untrusted"

REVOKED = "revoked"
NOT_TRUSTED = "not_trusted"
REVOKE_FAILED = "revoke_failed"

_WARNED: set[tuple[str, str]] = set()


# ---------- discovery --------------------------------------------------------


def get_project_hooks_settings_file(
    project_root: Optional[Path] = None,
) -> Optional[Path]:
    """Return ``<root>/.claude/settings.json`` if it exists, else ``None``.

    Discovery is CWD-only — no ancestor walk — so the ceremony can't be
    silently widened by a settings file in some parent directory.
    """
    root = _resolved_root(project_root)
    candidate = root / PROJECT_SETTINGS_RELPATH
    try:
        if not candidate.is_file():
            return None
        if candidate.is_symlink():
            logger.warning(
                "Refusing symlinked project hooks file %s — replace with a "
                "real file to enable the trust ceremony.",
                candidate,
            )
            return None
        resolved = candidate.resolve()
        if not _is_within(resolved, root):
            logger.warning(
                "Refusing project hooks file %s: resolves to %s, which is "
                "outside the project root. A parent directory (typically "
                "'.claude/') is likely a symlink. Replace with a real "
                "in-project directory to enable the trust ceremony.",
                candidate,
                resolved,
            )
            return None
        return candidate
    except OSError:  # pragma: no cover - exotic filesystem errors
        return None


def _is_within(path: Path, root: Path) -> bool:
    """True if *path* is *root* or lives beneath it. Both must be resolved."""
    try:
        return path.is_relative_to(root)
    except AttributeError:  # pragma: no cover - Python < 3.9
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False


def _resolved_root(project_root: Optional[Path]) -> Path:
    """Canonicalize the project root so a symlinked CWD and its real path
    produce the same trust key."""
    root = Path(project_root) if project_root is not None else Path.cwd()
    try:
        return root.resolve()
    except OSError:  # pragma: no cover - defensive
        return root


# ---------- content hashing --------------------------------------------------


def _extract_hooks_subtree(settings_file: Path) -> Optional[Dict[str, Any]]:
    """Return the ``hooks`` subtree of *settings_file*, or ``None``. Never raises."""
    try:
        raw_bytes = Path(settings_file).read_bytes()
    except OSError as exc:
        logger.warning("Could not read project hooks file %s: %s", settings_file, exc)
        return None
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        logger.warning("Non-UTF-8 project hooks file %s: %s", settings_file, exc)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON in project hooks file %s: %s", settings_file, exc)
        return None
    if not isinstance(data, dict):
        return None
    subtree = data.get("hooks")
    if not isinstance(subtree, dict):
        return None
    return subtree


def _has_effective_hooks(subtree: Dict[str, Any]) -> bool:
    """True iff *subtree* has a non-comment event entry.

    ``_``-prefixed keys are comments, matching the loader convention in
    :mod:`.config`.
    """
    return any(not key.startswith("_") for key in subtree)


def hash_subtree(subtree: Optional[Dict[str, Any]]) -> Optional[str]:
    """SHA-256 of the canonicalized subtree, or ``None`` if not effective.

    Takes a pre-parsed subtree so the loader can hash the exact bytes it is
    about to merge — re-reading the file would open a TOCTOU gap.
    """
    if subtree is None or not _has_effective_hooks(subtree):
        return None
    canonical = json.dumps(subtree, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_hooks_config_hash(settings_file: Path) -> Optional[str]:
    """Hash the hooks subtree as it exists on disk right now.

    Ceremony paths only. The loader must call :func:`hash_subtree` on an
    already-parsed subtree to stay TOCTOU-safe.
    """
    return hash_subtree(_extract_hooks_subtree(settings_file))


# ---------- trust store I/O --------------------------------------------------


def _project_key(project_root: Path) -> str:
    return str(_resolved_root(project_root))


def _load_store() -> dict:
    """Read the trust store, returning an empty store on any problem."""
    try:
        if TRUST_STORE_FILE.is_file():
            data = json.loads(TRUST_STORE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("projects"), dict):
                return data
            logger.warning(
                "Malformed hooks trust store at %s — treating as empty",
                TRUST_STORE_FILE,
            )
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Could not read hooks trust store %s: %s", TRUST_STORE_FILE, exc)
    return {"version": _STORE_VERSION, "projects": {}}


def _save_store(store: dict) -> bool:
    """Persist the trust store atomically. Returns ``False`` on failure.

    Written via tempfile + ``os.replace`` so a crash mid-write can't wipe
    every project's trust.
    """
    payload = json.dumps(store, indent=2, sort_keys=True)
    tmp_path: Optional[str] = None
    try:
        TRUST_STORE_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(TRUST_STORE_FILE.parent),
            prefix=TRUST_STORE_FILE.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_path = handle.name
        # This file decides whether shell commands run at boot, so keep it
        # owner-only rather than whatever the umask allows.
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, TRUST_STORE_FILE)
        return True
    except OSError as exc:
        logger.error("Could not write hooks trust store %s: %s", TRUST_STORE_FILE, exc)
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:  # pragma: no cover - defensive
                pass
        return False


# ---------- trust queries / mutations ----------------------------------------


def get_trust_status_for_hash(project_root: Path, current_hash: Optional[str]) -> str:
    """Compare a caller-provided hash to the store. TOCTOU-safe."""
    store = _load_store()
    entry = store["projects"].get(_project_key(project_root))
    if not isinstance(entry, dict) or not entry.get("hash"):
        return UNTRUSTED
    if current_hash is not None and current_hash == entry["hash"]:
        return TRUSTED
    return CHANGED


def get_trust_status(project_root: Path, settings_file: Path) -> str:
    """Trust status of *settings_file*, read from disk.

    Ceremony paths only — the loader uses :func:`get_trust_status_for_hash`.
    """
    return get_trust_status_for_hash(
        project_root, compute_hooks_config_hash(settings_file)
    )


def trust_project_hooks(project_root: Optional[Path] = None) -> bool:
    """Record acceptance of the current project's ``hooks`` subtree."""
    root = _resolved_root(project_root)
    settings_file = get_project_hooks_settings_file(root)
    if settings_file is None:
        return False
    hooks_hash = compute_hooks_config_hash(settings_file)
    if hooks_hash is None:
        return False
    store = _load_store()
    store["projects"][_project_key(root)] = {
        "hash": hooks_hash,
        "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return _save_store(store)


def revoke_project_hooks(project_root: Optional[Path] = None) -> str:
    """Remove trust for the current project.

    Returns :data:`REVOKED`, :data:`NOT_TRUSTED` (no entry to remove), or
    :data:`REVOKE_FAILED` (entry exists but the store could not be written,
    so the project is still trusted).
    """
    root = _resolved_root(project_root)
    store = _load_store()
    key = _project_key(root)
    if key not in store["projects"]:
        return NOT_TRUSTED
    del store["projects"][key]
    return REVOKED if _save_store(store) else REVOKE_FAILED


# ---------- boot / loader warning surface ------------------------------------


def warn_untrusted_project_hooks(
    project_root: Path, settings_file: Path, status: str
) -> None:
    """Emit a one-time warning about an unloaded project hooks file."""
    key = (_project_key(project_root), status)
    if key in _WARNED:
        return
    _WARNED.add(key)

    # The legacy message_queue's startup buffer is never drained, so
    # pre-renderer warnings emitted through it vanish silently.
    try:
        from code_puppy.messaging.bus import emit_warning
    except Exception:  # pragma: no cover - defensive during early boot
        return

    if status == CHANGED:
        emit_warning(
            f"Project hooks in '{settings_file}' have CHANGED since you "
            "trusted them — they will NOT run. Review the changes and run "
            "'/hooks trust accept' to re-enable."
        )
    else:
        emit_warning(
            f"Project hooks in '{settings_file}' are NOT trusted — they will "
            "NOT run. Review them and run '/hooks trust accept' to enable "
            "(project hooks can run arbitrary shell commands)."
        )


def _reset_warning_cache() -> None:
    """Test hook."""
    _WARNED.clear()


def emit_untrusted_project_hooks_warning_if_any(
    project_root: Optional[Path] = None,
) -> None:
    """Emit the untrusted-hooks warning, if any, for ``project_root``.

    Called from the ``startup`` plugin callback rather than from
    ``load_hooks_config`` so the warning renders after boot output instead
    of scrolling past above the banner.
    """
    root = _resolved_root(project_root)
    settings_file = get_project_hooks_settings_file(root)
    if settings_file is None:
        return

    subtree = _extract_hooks_subtree(settings_file)
    if subtree is None or not _has_effective_hooks(subtree):
        return

    current_hash = hash_subtree(subtree)
    status = get_trust_status_for_hash(root, current_hash)
    if status == TRUSTED:
        return

    warn_untrusted_project_hooks(root, settings_file, status)
