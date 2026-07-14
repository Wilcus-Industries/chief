"""iMessage self-DM send-record table (#161).

The durable half of the loop-proof echo filter: one ``imessage_sends`` row per own
send to a self-handle in self-DM mode, consumed (deleted) when its store echo
re-polls, so chief's own reply never re-dispatches. Content-keyed (send returns no
ROWID/guid), so the record survives a restart. No unique constraint or index —
single-user, low volume, autogenerate-drift-clean.

Revision ID: 0011
Revises:     0010
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "imessage_sends",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("handle", sa.String, nullable=False),
        sa.Column("body", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("imessage_sends")
