"""Self-edit: the agent changes its own config, prompts, and source —
only through the guarded pipeline (branch → done-check → restart →
healthcheck → rollback)."""
