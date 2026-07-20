# Installing obsidian-memory

Gives chief a long-term memory backed by an **Obsidian vault**: heading-chunked
notes embedded into a persistent vector index (model2vec + chromadb) for
semantic recall, a wikilink graph for "related notes", and an **owner-gated
ambient recall hook** that quietly surfaces relevant notes every few owner
turns. The package code already ships in core (the `chief_obsidian_memory`
subpackage under `packages/obsidian-memory/src/`); this install pulls in its
Python dependencies, configures the vault, and turns the hook on.

## Read before installing — the trade-offs

State these to the owner and get an explicit yes; do not install silently.

- **Heavy dependencies.** This adds `chromadb`, `model2vec`, `obsidiantools`
  and `networkx` (a few hundred MB, and a ~30MB embedding model downloaded on
  first index). They enter the project as dependencies of the shipped
  subpackage. The owner accepts this.
- **Owner-only by construction.** Ambient recall reads the owner's private
  vault, so the hook injects **nothing** on a non-owner turn (a monitor/cron
  `system` wake, a stranger). This is code-enforced, not policy.
- **The index lives outside the vault** (under `data/hooks/obsidian-memory/`),
  so indexing never writes into the notes. Recall is read-only unless the owner
  grants writable paths (below); capability follows that config.

## Steps

Gather the parameters through the interview (steps 1–5), add the dependencies
and config (steps 6–7), then build the first index and verify (steps 8–9).

1. **Vault mode.** Ask whether chief should use the owner's **existing** vault
   or **create a new one** for chief (which the owner can also open in
   Obsidian — a shared vault). Either way the answer is exactly **one** vault:
   the code indexes a single vault end-to-end (only the first `vault_paths`
   entry is used), so get one absolute path and never record a second.
2. **Sync.** Ask how the vault syncs across devices — **git** (default,
   recommended: versioned, diffable), **iCloud**, **Obsidian Sync**, or
   **none**. This is the owner's setup, not chief's; just record it so the skill
   can advise.
3. **Layout.** Read `docs/best-practices.md` with the owner and pick a note
   organization — PARA, Zettelkasten, MOC/atomic, or daily-notes. Fill in the
   gaps: which folders exist, where new notes go.
4. **Index scope.** Confirm the include/exclude prefixes. The defaults exclude
   `.obsidian/`, `templates/`, and `attachments/`; add any private folders the
   owner does not want recalled.
5. **Writable paths.** Ask whether chief may *save* notes and, if so, into which
   vault-relative folder(s) (e.g. `inbox/`, `chief/`). Empty means **read-only**
   recall — chief recalls but never writes. Capability follows this config.
6. **Add the Python dependencies (guarded self-edit).** With your file tools,
   add the subpackage to the project's runtime dependencies so the running
   daemon can import the hook, then sync:
   - Ensure `pyproject.toml` depends on `chief-obsidian-memory` (it ships as an
     editable path source under `[tool.uv.sources]`). If it is only in the dev
     group, add it to `[project.dependencies]` so production runs load it.
   - Run `uv sync` with the `shell` tool to install the vector stack.
7. **Write the config, deterministically.** Run
   `VAULT_PATH="<abs vault path>" WRITABLE_PATHS="<comma-sep dirs or empty>"
   bash packages/obsidian-memory/install.sh` with the `shell` tool. It copies
   the skill and sets `obsidian_memory.vault_paths` + `.writable_paths` via
   `chief.config_apply`, seeds the `memory-relevance` classifier definition
   into `classifiers/` (never overwriting an existing one), and records the
   install in `data/installed.yaml` via `chief.registry_apply`. Tune
   `obsidian_memory.ambient_n` (recall cadence), `.top_k` (candidates fetched,
   and so gate calls per firing), `.include`/`.exclude`, and
   `.injection_cap_tokens` with
   `uv run python -m chief.config_apply obsidian_memory.<key>=<value>` if the
   owner wants non-defaults — never append blocks to config.yaml by hand.
   The gate's *prompt and model* are not config: edit
   `classifiers/memory-relevance.md` (its `model:` frontmatter falls back to
   the `default_classifier` role).
8. **Port an existing memory store (best-effort, if present).** If a `memory`
   (or similar markdown-notes) package is installed, offer to copy its notes
   into the vault so nothing is lost. The competing package stays installed and
   usable — do not remove or modify it; the owner picks which to keep. Skip this
   step entirely if no such package exists.
9. **Build and verify.** `restart` to bring the config, deps, and hook live.
   Then build the first index and confirm recall:
   - `uv run chief-memory reindex --vault "<abs vault path>"` — reports the
     chunk count. (The first run downloads the embedding model.)
   - `uv run chief-memory search "<something in a note>" --vault "<path>"` —
     confirm the right note path comes back.
   - `uv run chief-memory related "<topic>" --vault "<path>"` and
     `uv run chief-memory links "<note>" --vault "<path>"` — confirm the
     wikilink neighbourhood.
   - Text chief a few times as the owner; on the cadence turn it should surface
     a relevant note in its context. Nothing surfaces on a non-owner turn.
