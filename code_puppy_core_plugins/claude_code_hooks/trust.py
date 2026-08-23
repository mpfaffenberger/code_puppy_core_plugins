"""Content-hash trust store for project-level Claude Code hook configs.

Project ``.claude/settings.json`` files can declare hooks that execute
arbitrary shell commands at Code Puppy lifecycle events (``SessionStart``,
``PreToolUse``, etc.). ``SessionStart`` in particular fires during boot,
which turns any hostile repo containing a crafted ``.claude/settings.json``
into a workstation-compromise vector the moment the user does
``cd <repo> && code-puppy``. So the project-level ``hooks`` subtree is
**disabled until the user explicitly accepts it** via the ``/hooks trust``
TUI ceremony, which records a SHA-256 of the canonicalized ``hooks`` subtree
here.

Trust model (identical philosophy to :mod:`code_puppy.plugins.trust` and
:mod:`code_puppy.mcp_.project_config`, deliberately not sharing code with
either — three trust surfaces do not yet justify a common base):

* **Store lives user-side** (``~/.code_puppy/trusted_hooks.json``) — never
  in the repo, so a repository can never self-trust.
* **Trust is content-addressed at subtree granularity.** Only the ``hooks``
  block of ``.claude/settings.json`` is security-relevant; other Claude Code
  settings in the same file are ignored by the hash. This avoids spurious
  re-prompts when unrelated settings change while still invalidating trust
  on any meaningful hook change.
* **Canonicalized JSON** (``sort_keys=True``, compact separators) is hashed
  so whitespace / key-order reformatting does not falsely invalidate trust.
  Array order **is** preserved — hook execution order is semantic.
* **Fail closed.** Unreadable store, malformed JSON, unhashable subtree, or
  a missing/empty/comment-only ``hooks`` block all resolve to "nothing to
  trust" and the project block is not merged into the effective config.
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

# User-side trust store, peer to trusted_plugins.json / trusted_mcp.json.
TRUST_STORE_FILE = Path.home() / ".code_puppy" / "trusted_hooks.json"

# Project-local Claude Code settings path, relative to CWD.
PROJECT_SETTINGS_RELPATH = Path(".claude") / "settings.json"

_STORE_VERSION = 1

# Trust statuses (mirror plugins.trust / mcp_.project_config for consistency).
TRUSTED = "trusted"  # stored hash exists and matches current subtree hash
CHANGED = "changed"  # stored hash exists but subtree has changed
UNTRUSTED = "untrusted"  # no stored hash — user never accepted this subtree

# Warn-once-per-session dedupe so a long-running session doesn't spam the
# same "found an untrusted project hooks config" message on every reload.
_WARNED: set[tuple[str, str]] = set()


# ---------- discovery --------------------------------------------------------


def get_project_hooks_settings_file(
    project_root: Optional[Path] = None,
) -> Optional[Path]:
    """Return ``<root>/.claude/settings.json`` if it exists, else ``None``.

    Discovery is intentionally CWD-only — no ancestor walk — because the
    trust ceremony is scoped to the user's current project. Ancestor
    lookups would silently widen the ceremony to whichever parent
    directory happens to contain a settings file, which is exactly the
    surprise we do not want when the user has just ``cd``'d into a
    freshly cloned repo.

    **Symlink hardening.** A hostile repo can bend the effective path in
    two ways: either the leaf ``.claude/settings.json`` itself is a
    symlink, or an intermediate component (typically ``.claude/``) is a
    symlink to attacker-controlled content. Both would let the file the
    user thinks they are reviewing ("the one in this repo") differ from
    the file the loader actually reads. We reject both by requiring:

    1. The leaf is not a symlink (``candidate.is_symlink()`` → refuse).
    2. The **resolved** absolute path is inside the resolved project
       root (``resolved.is_relative_to(root.resolve())`` → else refuse).

    Together this catches ``.claude/settings.json → anywhere``,
    ``.claude → attacker_dir``, and any relative traversal shenanigans.
    The user must lay down a real file in a real ``.claude`` directory
    inside the project to engage the trust ceremony.

    Parsing and semantic inspection (does the file actually contain a
    ``hooks`` block?) live in :func:`_extract_hooks_subtree` — this
    function is purely "does the file exist on disk?".
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
    """True if *path* is *root* or lives beneath it.

    Uses ``Path.is_relative_to`` when the caller's Python has it
    (3.9+); falls back to a prefix check otherwise. Both operands are
    expected to be already resolved (absolute, symlinks followed).
    """
    try:
        return path.is_relative_to(root)
    except AttributeError:  # pragma: no cover - Python < 3.9
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False


def _resolved_root(project_root: Optional[Path]) -> Path:
    """Canonicalize the project root once for every downstream caller.

    Resolving here (rather than sprinkling ``.resolve()`` across every
    entry point) keeps ``_project_key`` and the settings-path derivation
    consistent — a shell CWD reached via a symlink and its underlying
    real path both produce the same trust key.
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    try:
        return root.resolve()
    except OSError:  # pragma: no cover - defensive
        return root


# ---------- content hashing --------------------------------------------------


def _extract_hooks_subtree(settings_file: Path) -> Optional[Dict[str, Any]]:
    """Return the ``hooks`` subtree of *settings_file*, or ``None``.

    Returns ``None`` when:

    * The file cannot be read (permissions, disappearance mid-flight).
    * The file is not valid JSON.
    * The top level is not a JSON object.
    * The ``hooks`` key is absent, or present but not an object.

    Callers must treat ``None`` as "no trustable hooks here" (fail
    closed). This function never raises.
    """
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
    """True iff *subtree* has at least one non-comment event entry.

    Keys prefixed with ``_`` are treated as comments (matching the
    existing loader convention in :mod:`.config`). ``{}`` and configs
    containing only comment keys have nothing runnable and therefore
    nothing meaningful to gate — treating them as "nothing to trust"
    avoids inflicting ceremony over an empty bowl.
    """
    return any(not key.startswith("_") for key in subtree)


def hash_subtree(subtree: Optional[Dict[str, Any]]) -> Optional[str]:
    """SHA-256 of the canonicalized subtree, or ``None`` if not effective.

    Pure function — accepts a **pre-parsed** subtree so callers that
    already read the file (the loader is one) can hash the exact bytes
    they are about to merge, closing the TOCTOU gap that would exist if
    the file were re-read between check and use.

    Canonicalization uses ``json.dumps(subtree, sort_keys=True,
    separators=(",", ":"))`` so JSON reformatting (whitespace, key
    ordering) does not spuriously invalidate trust. Array order is
    preserved because hook execution order is semantic.
    """
    if subtree is None or not _has_effective_hooks(subtree):
        return None
    canonical = json.dumps(subtree, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_hooks_config_hash(settings_file: Path) -> Optional[str]:
    """I/O convenience: extract the subtree from disk and hash it.

    Thin wrapper over :func:`_extract_hooks_subtree` + :func:`hash_subtree`.
    Used by the ``/hooks trust accept`` ceremony where a fresh disk read
    is the correct semantics ("trust whatever is on disk *now*").

    The loader path deliberately does NOT use this — it hashes the same
    parsed subtree it is about to merge to avoid a TOCTOU race.
    """
    return hash_subtree(_extract_hooks_subtree(settings_file))


# ---------- trust store I/O --------------------------------------------------


def _project_key(project_root: Path) -> str:
    """Canonical store key for a project root (resolved absolute path)."""
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

    Uses ``tempfile.NamedTemporaryFile`` + :func:`os.replace` so a
    crash or Ctrl-C mid-write cannot leave an empty/half-written file
    behind. Losing every project's trust because the machine lost power
    during a save is fail-closed, but it is also a lousy UX; this makes
    saves durable and all-or-nothing.
    """
    payload = json.dumps(store, indent=2, sort_keys=True)
    tmp_path: Optional[str] = None
    try:
        TRUST_STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
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
        os.replace(tmp_path, TRUST_STORE_FILE)
        return True
    except OSError as exc:
        logger.error("Could not write hooks trust store %s: %s", TRUST_STORE_FILE, exc)
        # Best-effort cleanup: if the replace failed we still own the temp.
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:  # pragma: no cover - defensive
                pass
        return False


# ---------- trust queries / mutations ----------------------------------------


def get_trust_status_for_hash(project_root: Path, current_hash: Optional[str]) -> str:
    """TOCTOU-safe status check: compare a caller-provided hash to the store.

    The loader path uses this after computing the hash from the exact
    parsed subtree it is about to merge. Because the caller owns the
    bytes end-to-end, no second file read happens between check and
    merge.
    """
    store = _load_store()
    entry = store["projects"].get(_project_key(project_root))
    if not isinstance(entry, dict) or not entry.get("hash"):
        return UNTRUSTED
    if current_hash is not None and current_hash == entry["hash"]:
        return TRUSTED
    return CHANGED


def get_trust_status(project_root: Path, settings_file: Path) -> str:
    """I/O convenience: return the trust status of *settings_file*.

    Reads and hashes the file on disk. Suitable for ceremony paths
    (``/hooks trust status``) but NOT for the security-critical loader
    path — use :func:`get_trust_status_for_hash` there with a hash of
    the already-parsed subtree.

    Any failure to compute a current hash (unreadable file, malformed
    JSON, missing/empty ``hooks`` subtree) resolves to :data:`CHANGED`
    when the project has a stored entry, and to :data:`UNTRUSTED`
    otherwise. Both outcomes cause the loader to skip the project block,
    which is the fail-closed behavior we want.
    """
    return get_trust_status_for_hash(
        project_root, compute_hooks_config_hash(settings_file)
    )


def is_project_hooks_trusted(project_root: Optional[Path] = None) -> bool:
    """True only when the current project's hooks subtree is trusted & unchanged."""
    root = _resolved_root(project_root)
    settings_file = get_project_hooks_settings_file(root)
    if settings_file is None:
        return False
    return get_trust_status(root, settings_file) == TRUSTED


def trust_project_hooks(project_root: Optional[Path] = None) -> bool:
    """Record acceptance of the current project's ``hooks`` subtree.

    Returns ``False`` if there is no project settings file, no
    effective ``hooks`` subtree in it, the hash cannot be computed, or
    the trust store cannot be written.
    """
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


def revoke_project_hooks(project_root: Optional[Path] = None) -> bool:
    """Remove trust for the current project. Returns True if an entry existed."""
    root = _resolved_root(project_root)
    store = _load_store()
    key = _project_key(root)
    if key not in store["projects"]:
        return False
    del store["projects"][key]
    return _save_store(store)


# ---------- boot / loader warning surface ------------------------------------


def warn_untrusted_project_hooks(
    project_root: Path, settings_file: Path, status: str
) -> None:
    """Emit a one-time, actionable warning about an unloaded project hooks file.

    Deduped per ``(project_root, status)`` so a long-running session with
    engine reloads doesn't spam. Emits different phrasing for
    :data:`CHANGED` vs :data:`UNTRUSTED` because "you already trusted this,
    but the content moved under you" is a very different mental model
    from "we've never seen this file before".
    """
    key = (_project_key(project_root), status)
    if key in _WARNED:
        return
    _WARNED.add(key)

    # We import from ``code_puppy.messaging.bus`` (not the legacy
    # ``code_puppy.messaging.message_queue``) on purpose. This function is
    # called from ``load_hooks_config()`` which itself runs at plugin
    # **import time** — the module-level ``_hook_engine = _initialize_engine()``
    # in ``register_callbacks.py`` fires before the rich_renderer has
    # started. The legacy ``message_queue._startup_buffer`` has no drainer
    # (its public ``get_buffered_startup_messages`` helper is defined but
    # never called anywhere) so a warning emitted through it silently
    # evaporates. The bus's startup buffer, in contrast, is drained by
    # ``rich_renderer._consume_loop_sync`` when the renderer boots — which
    # is exactly the ordering we need for a pre-renderer emit to survive
    # and render.
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
    """Clear the warn-once cache. Test hook only."""
    _WARNED.clear()


def emit_untrusted_project_hooks_warning_if_any(
    project_root: Optional[Path] = None,
) -> None:
    """Re-derive project-hooks trust state and emit the warning if untrusted.

    Intended to be wired into the ``startup`` plugin callback so the warning
    fires **after** the ASCII-art banner and boot output have rendered —
    same visibility-maximising trick the truecolor warning uses (see
    ``cli_runner.py``: ``# Truecolor warning moved to interactive_mode() so
    it prints last — max visibility.``).

    Emitting from :func:`~.config.load_hooks_config` directly would fire at
    **plugin-import time**, before the renderer has drawn anything — the
    warning then scrolls off the top of the screen the instant the logo
    prints and the user never spots it.

    This function is a pure side-effect wrapper: it recomputes the same
    trust check ``load_hooks_config`` did (which is cheap — one JSON read
    plus one SHA-256) and, if the current state is anything other than
    :data:`TRUSTED`, emits the warn-once message. If the project has no
    ``.claude/settings.json``, no hooks subtree, or an empty hooks subtree,
    it does nothing.
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
