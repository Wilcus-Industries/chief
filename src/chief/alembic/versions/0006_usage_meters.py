"""Replace monthly_costs with per-currency usage_meters (#84, part of #72).

Budget rework: chief meters each turn in the native currency it actually spent — Copilot
premium requests, OpenRouter dollars, or bridge turns — instead of one Anthropic-dollar
month-to-date total. ``usage_meters`` holds one row per ``(cycle, currency)``; the old
single-currency ``monthly_costs`` table is dropped (no data carry-over — the currencies
don't convert).

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
    op.drop_table("monthly_costs")
    op.create_table(
        "usage_meters",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("cycle", sa.String, nullable=False),
        sa.Column("currency", sa.String, nullable=False),
        sa.Column("amount", sa.Float, nullable=False),
        sa.Column("mode", sa.String, nullable=False),
        sa.Column("warned_fraction", sa.Float, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint(
            "cycle", "currency", name="uq_usage_meter_cycle_currency"
        ),
    )


def downgrade() -> None:
    op.drop_table("usage_meters")
    op.create_table(
        "monthly_costs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("cycle", sa.String, nullable=False),
        sa.Column("total_cost_usd", sa.Float, nullable=False),
        sa.Column("mode", sa.String, nullable=False),
        sa.Column("warned_fraction", sa.Float, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("cycle", name="uq_monthly_cost_cycle"),
    )
