# Custom slash commands

Save reusable instructions as Markdown prompt commands. No skill metadata or
Python plugin is needed: you choose when to run the prompt.

## Create one from Code Puppy

```text
/commands create explain Explain the code, its assumptions, and edge cases.
/explain src/example.py
```

The first command saves `explain.md` in your global commands directory
(`<CONFIG_DIR>/commands`, normally `~/.code_puppy/commands`). It is immediately
available to run and appears in help/autocomplete. Text after `/explain` is
appended as **Additional context**; it is not substituted into the saved prompt.

To share a command with your project instead:

```text
/commands create --project review Review this diff for correctness and missing tests.
```

This writes `.agents/commands/review.md` in the current directory. Commit that
file if you want teammates to use it. Creation never overwrites an existing
file or knowingly collides with a registered command or advertised skill.
Use a lowercase name starting with a letter, followed by letters, digits,
underscores or hyphens (at most 64 characters).

## List and edit

```text
/commands
/commands list
```

Lists loaded custom commands with descriptions and the winning source file.
Edit that Markdown file in your editor for multi-line instructions. To remove
one, remove its file yourself. The helper intentionally has no delete or
shell-execution option.

## Existing Markdown files

The loader also reads `.claude/commands`, `.github/prompts` (`*.prompt.md`), and
`.agents/commands`. Project directories override global definitions; the list
shows the source that won. Subdirectories become namespaces: `team/review.md`
can be invoked as `/team/review`. The creation helper supports simple names;
you can still author namespaced files manually.

Optional `description:` frontmatter sets the help description. Normal command
bodies are sent to the model, not executed as scripts. The existing loader has
a separate `exec:` feature restricted to user-global trusted command files;
**the creation helper never creates an exec command**, even when supplied text
looks like frontmatter. Generated files start with a Markdown heading.

Skills are a different discovery mechanism: the model may discover and activate
them itself. Custom prompt commands are a simpler fit for instructions you want
to invoke explicitly.
