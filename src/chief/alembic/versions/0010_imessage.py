"""iMessage adapter tables (#156).

Three small tables behind the iMessage adapter: ``imessage_prefs`` (per-handle
delegation mode + first-send flag), ``unknown_senders`` (the metadata-only log of
non-whitelisted senders — handle and timestamps, never content), and
``adapter_cursors`` (the poller's persisted read position, so restarts neither
replay old texts nor drop ones that arrived while down).

Revision ID: 0010
Revises:     0009
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "imessage_prefs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("handle", sa.String, nullable=False),
        sa.Column("mode", sa.String, nullable=False),
        sa.Column("contacted", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("handle", name="uq_imessage_pref_handle"),
    )
    op.create_table(
        "unknown_senders",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("platform", sa.String, nullable=False),
        sa.Column("handle", sa.String, nullable=False),
        sa.Column("first_seen", sa.DateTime, nullable=False),
        sa.Column("last_seen", sa.DateTime, nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
        sa.UniqueConstraint(
            "platform", "handle", name="uq_unknown_platform_handle"
        ),
    )
    op.create_table(
        "adapter_cursors",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("platform", sa.String, nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("platform", name="uq_adapter_cursor_platform"),
    )


def downgrade() -> None:
    op.drop_table("adapter_cursors")
    op.drop_table("unknown_senders")
    op.drop_table("imessage_prefs")
