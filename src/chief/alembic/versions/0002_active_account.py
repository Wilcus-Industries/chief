"""Add active_account column to tasks (issue #45).

Per-thread active Google account binding: each conversation can pin one
registered account label, persisted here so the binding survives restarts
and never leaks between threads.

Revision ID: 0002
Revises:     0001
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("active_account", sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "active_account")
