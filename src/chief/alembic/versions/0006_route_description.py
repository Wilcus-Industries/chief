"""Add routes.description (issue #83, part of #72).

The self-config routing tool lets chief re-describe a category; the classifier folds
that free text into its prompt so an edit steers the next spawn. Additive + nullable,
so existing rows keep a NULL description and behave exactly as before.

Revision ID: 0006
Revises:     0005
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("routes", sa.Column("description", sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column("routes", "description")
