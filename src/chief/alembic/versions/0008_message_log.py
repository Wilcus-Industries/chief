"""Add the message_log table — the #133 outbound broadcast-bus record.

The #133 broadcast bus mirrors every stack's engine outbound onto the client-plane
socket and records each message here. Shaped so #132 can extend it for replay (role +
timestamps present; a ``surface`` column is deferred to #132). File rows carry only the
filename + caption — the attachment bytes are never persisted.

Revision ID: 0008
Revises:     0007
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "message_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("platform", sa.String, nullable=False),
        sa.Column("thread_key", sa.String, nullable=False),
        sa.Column("role", sa.String, nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("text", sa.String, nullable=False),
        sa.Column("filename", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("message_log")
