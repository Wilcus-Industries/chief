"""Add the routes table + tasks.route_category (issue #79, part of #72).

Model routing: ``routes`` holds one row per job category → ``{target_class, model}``
target (seeded from config on boot, the category set *is* the row set);
``route_category`` on ``tasks`` persists a per-task ``/route`` override so a routed
thread survives a restart.

Revision ID: 0005
Revises:     0004
Create Date: 2026-07-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "routes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("category", sa.String, nullable=False),
        sa.Column("target_class", sa.String, nullable=False),
        sa.Column("model", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("category", name="uq_route_category"),
    )
    op.add_column(
        "tasks", sa.Column("route_category", sa.String, nullable=True)
    )


def downgrade() -> None:
    op.drop_column("tasks", "route_category")
    op.drop_table("routes")
