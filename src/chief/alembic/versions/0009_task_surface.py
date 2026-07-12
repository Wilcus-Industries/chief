"""Add surface column to tasks (#135 review fix).

Persists each task's real ``Surface`` (home/dm/group) at creation so a cross-stack
inject onto a rebuilt task (post-restart or post-idle-archive, no live
``_RunningTask``) can use the thread's actual surface instead of assuming DM — the
assumption leaked owner approval cards into a shared GROUP room. ``NULL`` on a
pre-existing row means "unproven"; callers must fail closed on that, never default it
to DM.

Revision ID: 0009
Revises:     0008
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("surface", sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "surface")
