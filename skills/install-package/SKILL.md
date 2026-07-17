---
name: install-package
description: Install a package (manifest + INSTALL.md) the agent-driven way.
---

# Installing a package

Packages are directories with a `manifest.yaml` and an `INSTALL.md`. Install is
a partnership: the `install_package` tool does the deterministic bytes (copying
skill files verbatim, setting standard config keys, wiring MCP servers) and you
do the judgment, interactive, and customizable parts from the INSTALL.md. Never
retype a skill file yourself — that is exactly what the tool exists to prevent.

Recipe:

1. Discover what is available with `read_file`/`grep` — there is no package-list
   tool. Bundled packages live under `packages/`, cloned ones under
   `data/packages/`; each is a dir with a `manifest.yaml`. `grep` for
   `"description:"` under those roots (or `read_file` a specific
   `manifest.yaml`) to see names and one-liners. If the package the owner wants
   is not there, clone the packages repo
   (`https://github.com/CrazyWillBear/chief-packages`) into `data/packages/`
   first, then look again.
2. `read_file` the package's `manifest.yaml` (dependencies, mcp_servers, skills,
   config_keys, secrets) and its `INSTALL.md`. Dependencies install first — the
   `install_package` tool resolves and orders the whole tree for you.
3. Read the INSTALL.md. Anything it marks as a question — setup mode, handles,
   notify tier, secrets — gather from the owner **first**, conversationally,
   before running anything. A public-facing package always pulls in `screening`
   as a dependency; the tool installs it for you — never skip it.
4. Call `install_package` **once** with the package name and an `env` object
   holding the parameters INSTALL.md asked for (e.g.
   `{"IMESSAGE_HANDLES": "+15551234567"}`). This runs every install.sh in the
   dependency tree under one approval and one done-check: it copies the skill
   dirs byte-for-byte and sets the standard config keys. If it returns an
   error, fix the inputs and retry — do not fall back to hand-copying files.
5. Do the INSTALL.md's remaining steps yourself — the ones a script can't or
   shouldn't own: OS-interactive prompts (OAuth, permission grants) and
   **customizable** pieces (e.g. a notify-tier monitor built from the sample
   snippets in the skill/INSTALL.md, adapted to the owner). For `secrets`, ask
   the owner to place each file under `secrets/` themselves — never have them
   paste secret values into chat.
6. When done, confirm the capability works with a real call before reporting
   success.
