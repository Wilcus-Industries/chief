"""Watch candidates + unbound watches (#168, part of PRD #160).

Widens ``watches.target_handle`` to nullable (``None`` = unbound, awaiting owner
confirmation of an unknown sender) and adds ``confirmed_at``. Adds the new
``watch_candidates`` table: one metadata-only sighting row per (watch, handle),
surfaced to the owner and resolved via confirm/reject.  SQLite needs a batch
(table-rebuild) ALTER to widen a column's nullability, mirroring
0004_drop_spinner_msg_ref.py.

Revision ID: 0013
Revises:     0012
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("watches") as batch:
        batch.alter_column("target_handle", existing_type=sa.String, nullable=True)
        batch.add_column(sa.Column("confirmed_at", sa.DateTime, nullable=True))
    op.create_table(
        "watch_candidates",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("watch_id", sa.Integer, sa.ForeignKey("watches.id"), nullable=False),
        sa.Column("handle", sa.String, nullable=False),
        sa.Column("first_seen", sa.DateTime, nullable=False),
        sa.Column("decision", sa.String, nullable=False),
        sa.Column("decided_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint(
            "watch_id", "handle", name="uq_watch_candidate_watch_handle"
        ),
    )


def downgrade() -> None:
    op.drop_table("watch_candidates")
    with op.batch_alter_table("watches") as batch:
        batch.drop_column("confirmed_at")
        batch.alter_column("target_handle", existing_type=sa.String, nullable=False)
