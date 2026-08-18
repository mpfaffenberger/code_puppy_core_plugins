# Code Puppy Core Plugins

Official plugin bundle for [Code Puppy](https://github.com/mpfaffenberger/code_puppy).

This distribution contains Code Puppy's builtin feature plugins. The core runtime
discovers them through the `code_puppy.plugins` Python entry-point group, so the
runtime remains independent of plugin implementation modules.

## Installation

Normal users do not need to install this package directly: `code-puppy` depends
on it and installs a compatible version automatically.

For development:

```bash
git clone https://github.com/mpfaffenberger/code_puppy.git

git clone https://github.com/mpfaffenberger/code_puppy_core_plugins.git
cd code_puppy_core_plugins
uv sync
uv pip install -e ../code_puppy
uv pip install -e .
uv run pytest
```

## Architecture

Each plugin is a package under `code_puppy_core_plugins/` with one
`register_callbacks.py`. Package metadata advertises those modules as entry
points. Code Puppy loads the entry points as its builtin tier, followed by user
plugins and trusted project plugins.

The package deliberately does not depend on `code-puppy` in its own metadata.
`code-puppy` depends on this bundle; adding the reverse edge would create a
packaging dependency cycle. Plugin CI installs the core runtime separately.

## Completion notifications

The `completion_notification` plugin is disabled by default. Enable a native,
best-effort desktop notification after a successful top-level response with:

```text
/set completion_notifications=true
```

Notifications never include response text. When available, the notification
labels Apple Terminal, iTerm2, or Warp using `TERM_PROGRAM`; unknown terminals
get a generic message. Sub-agent completions are intentionally silent.

Optional sound configuration is platform-aware:

```text
/set completion_notification_sound=Frog
```

On macOS, a simple name selects a system sound (`Frog`, `Glass`, `Ping`, etc.).
Preview one with `afplay /System/Library/Sounds/Frog.aiff`. On any platform, an
absolute path to a local sound file is accepted when an available platform
player can play it. Linux uses `notify-send` for desktop notifications when it
is installed; Windows uses a best-effort PowerShell toast. No external Python
dependencies are required.

## Releases

Pushes to `main` run lint and tests, bump the patch version, build, publish to
PyPI as `code-puppy-core-plugins`, and push the version tag. Configure the
`PYPI_API_TOKEN` repository secret before the first release.

## License

MIT
