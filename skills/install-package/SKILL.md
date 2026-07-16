---
name: install-package
description: Install a package (manifest + INSTALL.md) the agent-driven way.
---

# Installing a package

Packages are directories with a `manifest.yaml` and an `INSTALL.md`. There is
no package manager — you are the installer. Follow this recipe:

1. Call `list_packages` to see what is available. If the package the owner
   wants is not listed, clone the packages repo (URL in the tool output) into
   `data/packages/` using your tools, then list again.
2. Call `package_info` with the package name. It returns the dependency-ordered
   install list (dependencies first), the manifest, and the INSTALL.md.
3. Install each package in that order. A public-facing package always depends
   on `screening` — never skip it.
4. For each package, follow its INSTALL.md step by step. The manifest tells
   you what the steps will need:
   - `mcp_servers`: wire each with the `add_mcp_server` tool.
   - `skills`: copy each listed skill directory into `skills/` with the
     `self_edit` tool (write the SKILL.md content at the new path).
   - `config_keys`: set them in `config.yaml` via `self_edit`.
   - `secrets`: ask the owner to place each secret file under `secrets/`
     themselves — never ask them to paste secret values into chat.
5. Anything interactive (OAuth dances, OS permission prompts) — walk the owner
   through it conversationally, one step at a time, confirming each worked.
6. When done, confirm the capability works with a real call before reporting
   success.
