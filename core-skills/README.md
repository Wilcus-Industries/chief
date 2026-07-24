Core's own skills, tracked — the **source** copies. One directory per skill
with a SKILL.md (frontmatter: name, description; body: instructions).

The **installed** copies live in `skills/`, which is untracked instance data
(like `config.yaml`, `secrets/`, and `data/`): chief edits its own installed
skills, and tracking them made every release collide with that. Boot seeds a
core skill from here only when it is missing, so an edited copy is never
overwritten. Packages copy their own skills into `skills/` the same way, and
the agent can author its own there via self-edit.
