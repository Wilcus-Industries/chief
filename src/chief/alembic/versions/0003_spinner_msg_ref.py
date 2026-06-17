"""Add spinner_msg_ref column to tasks (issue #68).

Persists the in-flight spinner handle (the platform status-message reference) so
recovery after a process crash can delete any orphaned spinner that was left
ticking in a chat.  Cleared on clean turn completion; NULL means no spinner is
in flight.

Revision ID: 0003
Revises:     0002
Create Date: 2026-06-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("spinner_msg_ref", sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "spinner_msg_ref")
