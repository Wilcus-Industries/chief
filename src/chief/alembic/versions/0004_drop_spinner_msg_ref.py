"""Drop the spinner_msg_ref column from tasks (spinner removed).

The ticking ⏳ spinner (issues #66/#68) was removed: owner turns now stay silent
until the reply, so the persisted in-flight spinner reference has no readers left.
Drop the column.  SQLite needs a batch (table-rebuild) ALTER, so this uses
``batch_alter_table`` rather than a bare ``op.drop_column``.

Revision ID: 0004
Revises:     0003
Create Date: 2026-06-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("spinner_msg_ref")


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("spinner_msg_ref", sa.String, nullable=True))
