"""Cross-version callback-registry isolation for core-plugin tests."""

from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def preserve_callback_registry(callbacks):
    """Preserve priorities on new Code Puppy and fall back on legacy state."""
    snapshotter = getattr(callbacks, "snapshot_callback_registry", None)
    restorer = getattr(callbacks, "restore_callback_registry", None)
    if callable(snapshotter) and callable(restorer):
        snapshot = snapshotter()
        try:
            yield
        finally:
            restorer(snapshot)
        return

    saved_callbacks = {
        phase: list(functions) for phase, functions in callbacks._callbacks.items()
    }
    saved_owners = dict(callbacks._callback_owners)
    saved_loading = callbacks._current_loading_plugin
    try:
        yield
    finally:
        callbacks._callbacks.clear()
        callbacks._callbacks.update(saved_callbacks)
        callbacks._callback_owners.clear()
        callbacks._callback_owners.update(saved_owners)
        callbacks._current_loading_plugin = saved_loading
