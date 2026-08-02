Subagent definitions live here: one <name>.md per agent
(frontmatter: name, description, tools allowlist, optional model;
body: its system prompt).

`tools:` is default-closed — omit it and the agent gets NO tools. Name every
tool it needs, e.g. `tools: [read_file, grep]`. `spawn_agent` is always
excluded, so subagents cannot spawn further subagents.

Subagent tool calls go through the same gate as the owner's own: cards raise
on the parent's thread, `gate.never` still denies, and every call is audited
with the agent's name. See docs/SECURITY.md.
