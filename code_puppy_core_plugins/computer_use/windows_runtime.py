"""Keep all UIA objects on one COM-initialized worker, never on the event loop."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from .backend_types import ComputerUseError


class WindowsRuntime:
    """Synchronous backend facade used inside the shared async tool dispatcher.

    UIA wrappers never execute on arbitrary asyncio.to_thread workers. All native
    calls for this backend, including complete batches, execute on one apartment.
    """

    def __init__(self):
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="computer-use-uia"
        )
        self._backend = None
        self._owner = None
        self.cancelled = threading.Event()

    def _initialize(self):
        if self._backend is None:
            try:
                import comtypes

                comtypes.CoInitializeEx()
            except ImportError as exc:
                raise ComputerUseError(
                    "Install the Windows dependencies in this environment: "
                    "pip install 'code-puppy-core-plugins[computer-use]'"
                ) from exc
            from .windows_backend import WindowsBackend

            self._backend = WindowsBackend(cancelled=self.cancelled)
            self._owner = threading.get_ident()

    def _invoke(self, name, args, kwargs):
        self._initialize()
        if self.cancelled.is_set():
            self._backend.states.clear()
            raise ComputerUseError("Computer Use request cancelled; fetch fresh state")
        return getattr(self._backend, name)(*args, **kwargs)

    def _request(self, function, args, kwargs, cancel):
        self._initialize()
        self.cancelled = cancel
        self._backend.cancelled = cancel
        if cancel.is_set():
            raise ComputerUseError("Computer Use request cancelled before execution")
        try:
            return function(*args, **kwargs)
        finally:
            if cancel.is_set():
                self._backend.states.clear()

    def run_request(self, function, args, kwargs, cancel):
        return self._executor.submit(
            self._request, function, args, kwargs, cancel
        ).result()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            if threading.get_ident() == self._owner:
                return self._invoke(name, args, kwargs)
            return self._executor.submit(self._invoke, name, args, kwargs).result()

        return call
