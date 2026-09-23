"""The two judge questions, in the systemone wire format.

Any model that speaks this shape works: TypeSafe Jev, Kev, or a successor.

Kept identical to jev-proxy's ``src/router/questions.ts`` so both route the
same way. Field names in backticks refer to keys of the state built in
``state.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List

COMPLEXITY_LEVELS: List[Dict[str, Any]] = [
    {
        "what": (
            "Trivial. A one-line or single-spot change with an obvious answer: rename a symbol, "
            "fix a typo, tweak formatting, add a log line, answer a quick question about syntax or "
            "a command, or acknowledge a tool result. A small model does this reliably."
        ),
        "examples": [
            "rename `getUser` to `fetchUser`",
            "add a semicolon / fix this lint error",
            "what flag makes grep case-insensitive?",
            "run the tests again",
        ],
    },
    {
        "what": (
            "Routine. One clearly specified change inside a single file or function with a "
            "well-known pattern: write a small helper, add a field and thread it through, write "
            "unit tests for an existing function, explain what a short piece of code does, convert "
            "a config format."
        ),
        "examples": [
            "write a function that parses ISO dates and returns epoch millis",
            "add a `--verbose` flag to this CLI",
            "write pytest tests for `truncate_middle`",
            "explain what this regex matches",
        ],
    },
    {
        "what": (
            "Involved. Needs reasoning across several files or components, or a non-trivial "
            "decision: implement a feature that touches a few modules, debug a failure whose cause "
            "is not in the error message, refactor with behaviour preserved, review a change for "
            "problems, or choose between reasonable implementations."
        ),
        "examples": [
            "add pagination to the users endpoint including the client, the API handler, and the tests",
            "this test fails intermittently; here are the three files involved, find out why",
            "refactor the config loading into a separate module without changing behaviour",
            "review this diff for bugs",
        ],
    },
    {
        "what": (
            "Expert. System or architecture design, hard algorithms or performance work, subtle "
            "concurrency, correctness, or security bugs, large multi-module implementations, "
            "migrations of live data, or the user explicitly asks for deep, careful, rigorous "
            "reasoning."
        ),
        "examples": [
            "design the data model and API for multi-tenant billing",
            "find the race condition in this scheduler",
            "make this query 10x faster without changing results",
            "think very carefully and double-check every step",
        ],
    },
]

ROUTING_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "complexity": {
        "type": "score",
        "instructions": {
            "question": (
                "How demanding is the `request` for an AI coding assistant to complete correctly? "
                "Use `system_prompt_excerpt` and `prior_assistant_excerpt` only as context for what "
                "the request means."
            ),
            "focus": (
                "Judge the difficulty of producing a correct, complete result, not the length of the "
                "request. A short follow-up inside a larger task belongs to the difficulty of that task."
            ),
        },
        "criteria": COMPLEXITY_LEVELS,
    },
    "risky": {
        "type": "noul",
        "instructions": (
            "Would a mistake while carrying out the `request` be costly or hard to notice? Consider "
            "data loss, security or authentication, payments, database migrations, concurrency, "
            "deleting or overwriting files, and production configuration."
        ),
        "criteria": {
            "true": {
                "what": "The change touches something where an error causes damage or slips through review.",
                "examples": [
                    "rewrite the auth middleware",
                    "write the migration that drops the old column",
                    "fix the deadlock in the worker pool",
                    "change the deploy script",
                ],
            },
            "false": {
                "what": "A mistake is cheap and obvious: it fails a test, a build, or a glance.",
                "examples": [
                    "rename a variable",
                    "add a unit test",
                    "explain this function",
                    "adjust log formatting",
                ],
            },
        },
    },
}
