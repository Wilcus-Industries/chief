"""Watches table (#165, part of PRD #160).

One ``watches`` row per standing instruction over a resolved iMessage handle: the
target, the instruction text, the reporting tone, the lifecycle ``state``, and the
expiry the firing milestone will enforce. This slice only creates, lists, and
cancels — nothing fires yet.

Revision ID: 0012
Revises:     0011
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "watches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("target_handle", sa.String, nullable=False),
        sa.Column("instruction", sa.String, nullable=False),
        sa.Column("tone", sa.String, nullable=False),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("expiry", sa.DateTime, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("watches")
