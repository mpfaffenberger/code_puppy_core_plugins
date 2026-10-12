"""Tab completion for ``/router``: subcommands, ``init`` flags, and model names."""

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

from termflow.tui.completion import Completer, Completion, Document

from code_puppy.command_line.completion_cache import TTLCache

TRIGGER = "/router"

SUBCOMMANDS: Dict[str, str] = {
    "status": "config, key, last decision",
    "models": "registered model names usable as tiers",
    "init": "write a starting config",
    "route": "dry run a prompt through the judge",
    "last": "the last decision in full",
    "log": "recent routed runs",
    "on": "enable routing",
    "off": "disable routing",
    "reload": "re-read the config file",
}

MODEL_FLAGS: Dict[str, str] = {
    "--flagship": "strongest model; also the fallback",
    "--mid": "model for involved work",
    "--cheap": "model for trivial and routine turns",
}
OTHER_FLAGS: Dict[str, str] = {"--force": "overwrite an existing config"}

_catalogue_cache: TTLCache[dict] = TTLCache()


def _catalogue() -> dict:
    from code_puppy.model_factory import ModelFactory

    return _catalogue_cache.get(ModelFactory.load_config)


def model_choices() -> List[Tuple[str, str]]:
    """(name, type) for every registered model except the virtual one."""
    from .register_callbacks import MODEL_TYPE

    choices: List[Tuple[str, str]] = []
    for name, entry in sorted(_catalogue().items()):
        model_type = str(entry.get("type", "")) if isinstance(entry, dict) else ""
        if model_type == MODEL_TYPE:
            continue
        choices.append((name, model_type))
    return choices


def _matches(candidate: str, partial: str) -> bool:
    return not partial or partial.lower() in candidate.lower()


class RouterCompleter(Completer):
    """Completes ``/router <subcommand>`` and ``/router init --flag <model>``."""

    trigger = TRIGGER

    def get_completions(
        self, document: Document, complete_event: object
    ) -> Iterable[Completion]:
        text = document.text_before_cursor.lstrip()
        if not text.startswith(self.trigger + " "):
            return
        after = text[len(self.trigger) + 1 :]
        tokens = after.split()
        at_word_boundary = after.endswith(" ") or not after
        current = "" if at_word_boundary else tokens[-1]
        start = -len(current)

        # First word: the subcommand.
        if not tokens or (len(tokens) == 1 and not at_word_boundary):
            for name, meta in SUBCOMMANDS.items():
                if _matches(name, current):
                    yield Completion(
                        name, start_position=start, display=name, display_meta=meta
                    )
            return

        if tokens[0] != "init":
            return

        previous = (
            tokens[-2]
            if not at_word_boundary and len(tokens) > 1
            else (tokens[-1] if at_word_boundary else None)
        )
        if previous in MODEL_FLAGS:
            for name, model_type in model_choices():
                if _matches(name, current):
                    yield Completion(
                        name,
                        start_position=start,
                        display=name,
                        display_meta=model_type,
                    )
            return

        used = set(tokens[1:] if at_word_boundary else tokens[1:-1])
        for flag, meta in {**MODEL_FLAGS, **OTHER_FLAGS}.items():
            if flag not in used and _matches(flag, current):
                yield Completion(
                    flag, start_position=start, display=flag, display_meta=meta
                )
