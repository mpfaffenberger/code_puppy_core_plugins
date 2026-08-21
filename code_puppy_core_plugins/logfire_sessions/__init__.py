"""Mirror Code Puppy chat sessions into Logfire as structured log records.

The record schema lives under the ``cp.hist.*`` namespace -- deliberately NOT
``cp.session.*``. Logfire's hosted MCP query path scrubs keys and values that
contain sensitive keywords, and "session" is on that list, which would render
every listing useless and corrupt restored payloads.
"""
