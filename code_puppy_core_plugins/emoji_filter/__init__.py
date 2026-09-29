"""Strip emojis from agent outputs and file writes.

Targets (and nothing else):
* Streaming TextPart / TextPartDelta content (rendered to the terminal).
* File-write tool args (create_file, edit_file, replace_in_file).
* Shell command strings (shell).

Explicitly excluded: ThinkingPart / ThinkingPartDelta, banners, emit_* messages,
search strings (old_str / snippet), and anything else not listed above.

Toggle via the ``emoji_filter`` key in puppy.cfg. Default: on.
"""
