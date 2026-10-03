"""Private prompt handoff and bounded shell bootstrap commands."""

import os
import tempfile
from pathlib import Path

# Keep terminal input short and single-line, even before the shell editor starts.
# The child owns deletion once it reads the file, before importing the CLI.
BOOTSTRAP = (
    "import runpy,sys,os;"
    "p=sys.argv[1];"
    "t=open(p,encoding='utf-8').read();"
    "os.unlink(p);"
    "sys.argv=['code_puppy',*sys.argv[2:],'--',t];"
    "runpy.run_module('code_puppy',run_name='__main__',alter_sys=True)"
)


def prompt_file(prompt):
    """Create an exclusive private UTF-8 handoff; never reuse a user file."""
    descriptor, name = tempfile.mkstemp(prefix="puppy-herdr-", suffix=".txt")
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
            file.write(prompt)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path
