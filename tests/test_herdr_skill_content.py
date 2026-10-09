"""Keep the canonical agent playbook complete without bloating load_prompt."""

from pathlib import Path

from code_puppy_core_plugins.herdr import command_intent


def test_playbook_preserves_operational_safety():
    body = " ".join(
        Path(command_intent.__file__)
        .with_name("SKILL.md")
        .read_text(encoding="utf-8")
        .split()
    )
    # Deliberate wording tripwires: these operational limits and prohibitions
    # must not silently erode. Intentional rewording needs a safety review of
    # the matching assertion, not a weaker length/structure proxy.
    for phrase in (
        "before controlling sibling panes",
        "/herdr spawn NAME --prompt-file PATH",
        "multiword/multiline",
        "under 1,000 UTF-8 bytes",
        "tokens beginning with `/` or `!`",
        "absolute-path prose",
        "sender candidates",
        "aliases",
        "custom-plugin help",
        "not a target manifest",
        "Recognition is not a safety guarantee",
        "Never probe by executing commands",
        "user intentionally authorized",
        "`/exit`, `/clear`, `/truncate`",
        "foreground process",
        "not editor readiness",
        "not atomic",
        "not confirm child receipt, execution, or completion",
        "not byte-preserved",
        "remove quotes, normalize spaces, and consume attachments",
        "slow child",
        "inspect it",
        "Never auto-retry",
        "current installed interpreter",
        "launcher import execute",
        "do not substitute raw",
    ):
        assert phrase in body
