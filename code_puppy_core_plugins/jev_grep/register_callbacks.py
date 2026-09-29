"""smart_grep: semantic code search judged by TypeSafe's Jev decision model.

Registers a ``smart_grep`` tool alongside the regular ``grep``. It is opt-in:
agents only see the tool, and the smart_grep-first discovery policy is only
added to the prompt, once ``/set smart_grep on`` is set AND a Jev/TypeSafe
key is configured. ``/set`` reloads the agent, so toggling applies at once.
"""

from code_puppy.callbacks import register_callback
from code_puppy_core_plugins.plugin_settings import register_settings

from .config import SETTINGS, is_available
from .tool import TOOL_NAME, register_smart_grep


def _register_tools():
    return [{"name": TOOL_NAME, "register_func": register_smart_grep}]


def _advertise_when_configured(agent_name=None):
    return [TOOL_NAME] if is_available() else []


def _discovery_instructions() -> str | None:
    """Same live gate as tool advertisement; the key itself is never included."""
    if not is_available():
        return None
    return """## Code discovery: smart_grep first
When smart_grep is available, use it as your first tool for gathering code
context about an unfamiliar implementation. Describe the behavior you need to
find in plain English, scoped to the relevant repository or directory. Do this
instead of guessing symbol names, running exploratory grep chains, or listing
and reading many files just to locate the implementation.

Use the returned source excerpts and line ranges to decide which files need a
targeted read. Do not automatically read every match or repeat searches when
the excerpts already answer the question. Read the relevant existing code before
editing; semantic search is discovery, not a substitute for understanding it.

Use regular grep for exact symbols, regexes, and exhaustive references. Read a
known file directly when its location is already established. Use list_files
when you actually need directory contents, not as a ritual before every search.

A lexical shortlist is not exhaustive and no matches do not prove absence.
If results are weak, rephrase or narrow the query, or use exact grep as needed.
If smart_grep is unavailable or errors, fall back to grep and targeted reads;
do not block the task or repeatedly retry a missing key or failing provider.
Respect requests to keep code local: smart_grep sends selected source, paths,
and the query to TypeSafe, so use local grep/reads for local-only work.
"""


register_callback("load_prompt", _discovery_instructions)
register_callback("register_tools", _register_tools)
register_callback("register_agent_tools", _advertise_when_configured)
register_settings(SETTINGS)
