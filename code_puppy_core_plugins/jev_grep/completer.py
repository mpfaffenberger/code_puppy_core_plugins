"""Tab-completion for smart_grep's ``/set`` keys.

Core's ``/set`` completer only knows its built-in keys plus whatever is
already saved in puppy.cfg, so ``smart_grep`` was invisible until typed blind.
This fills that gap and steps aside once a key is saved: core lists it from
then on, and yielding it here too would show it twice.
"""

from __future__ import annotations

from typing import Iterable

from termflow.tui.completion import Completer, Completion, Document

from code_puppy.config import get_config_keys

from .config import SETTING_KEYS

TRIGGER = "/set "


class SmartGrepSetCompleter(Completer):
    """Completes unsaved smart_grep keys after ``/set ``."""

    def get_completions(
        self, document: Document, complete_event: object
    ) -> Iterable[Completion]:
        text = document.text_before_cursor.lstrip()
        if not text.startswith(TRIGGER):
            return
        partial = text[len(TRIGGER) :].lstrip()
        candidates = [key for key in SETTING_KEYS if key.startswith(partial)]
        if not candidates:
            return  # don't touch puppy.cfg for every unrelated `/set` keystroke
        saved = set(get_config_keys())
        for key in candidates:
            if key in saved:
                continue
            # Same "key = " shape core's SetCompleter emits for unset keys.
            yield Completion(f"{key} = ", start_position=-len(partial))
