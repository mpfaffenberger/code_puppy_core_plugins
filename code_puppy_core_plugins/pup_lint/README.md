# Pup Lint integration

This optional plugin injects a structured `pup_lint` tool into Code Puppy agents
when [Pup Lint](https://github.com/kvandre12-commits/pup-lint) is importable in
the Code Puppy environment or its `pup-lint` executable is on `PATH`.

Install the universal, dependency-free wheel in the environment that launches
Code Puppy:

```sh
python -m pip install \
  https://github.com/kvandre12-commits/pup-lint/releases/download/v0.1.0/pup_lint-0.1.0-py3-none-any.whl
```

The model-facing tool is intentionally diagnostic-only. It does not expose the
CLI's `--fix` option, so model-initiated file changes continue to pass through
Code Puppy's normal file-permission and review mechanisms.
