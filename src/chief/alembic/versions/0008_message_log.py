"""Add the per-thread message_log table for detach-replay (#132).

The CLI stack (and, later, every stack) logs both directions of its traffic into
``message_log``: inbound owner messages and outbound chief frames. An outbound row's
``delivered`` snapshots whether a client was attached at emit time, so a client that
reattaches after a detached period replays the undelivered rows (the log is the
held-message mechanism — no separate outbox). The ``(platform, delivered)`` index keeps
that replay claim cheap.

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
        sa.Column("surface", sa.String, nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("text", sa.String, nullable=False),
        sa.Column("payload", sa.String, nullable=True),
        sa.Column("delivered", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index(
        "ix_message_log_platform_delivered",
        "message_log",
        ["platform", "delivered"],
    )


def downgrade() -> None:
    op.drop_index("ix_message_log_platform_delivered", table_name="message_log")
    op.drop_table("message_log")
