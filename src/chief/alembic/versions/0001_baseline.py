"""Baseline revision — full schema from initial models.

Captures every table in Base.metadata as of M0–M11: contacts, tasks, approvals,
policy, rate_limits, monthly_costs, schedules.

Revision ID: 0001
Revises:     (none — fresh baseline)
Create Date: 2026-06-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "contacts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("platform", sa.String, nullable=False),
        sa.Column("user_id", sa.String, nullable=False),
        sa.Column("display_name", sa.String, nullable=True),
        sa.Column("tier", sa.String, nullable=False),
        sa.Column("admitted", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("state", sa.String, nullable=False, server_default="pending"),
        sa.Column("namespace", sa.String, nullable=False),
        sa.Column("first_seen", sa.DateTime, nullable=False),
        sa.UniqueConstraint("platform", "user_id", name="uq_contact_platform_user"),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("platform", sa.String, nullable=False),
        sa.Column("thread_key", sa.String, nullable=False),
        sa.Column("tier", sa.String, nullable=False),
        sa.Column("subject_id", sa.String, nullable=True),
        sa.Column("status", sa.String, nullable=False, server_default="open"),
        sa.Column("model", sa.String, nullable=True),
        sa.Column("title", sa.String, nullable=True),
        sa.Column("sdk_session_id", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("platform", "thread_key", name="uq_task_platform_thread"),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("payload_preview", sa.String, nullable=True),
        sa.Column("state", sa.String, nullable=False, server_default="requested"),
        sa.Column("decided_by", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("decided_at", sa.DateTime, nullable=True),
    )
    op.create_table(
        "policy",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("list_name", sa.String, nullable=False),
        sa.Column("tool", sa.String, nullable=False),
        sa.Column("arg_pattern", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_table(
        "rate_limits",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("scope", sa.String, nullable=False),
        sa.Column("window_start", sa.DateTime, nullable=False),
        sa.Column("count", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint("scope", name="uq_rate_limit_scope"),
    )
    op.create_table(
        "monthly_costs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("cycle", sa.String, nullable=False),
        sa.Column(
            "total_cost_usd", sa.Float, nullable=False, server_default="0.0"
        ),
        sa.Column("mode", sa.String, nullable=False, server_default="normal"),
        sa.Column("warned_fraction", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("cycle", name="uq_monthly_cost_cycle"),
    )
    op.create_table(
        "schedules",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("spec", sa.String, nullable=False),
        sa.Column("action", sa.String, nullable=True),
        sa.Column("action_type", sa.String, nullable=False),
        sa.Column("thread_key", sa.String, nullable=True),
        sa.Column("urgent", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("next_run", sa.DateTime, nullable=True),
        sa.Column("last_run", sa.DateTime, nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("predicate", sa.String, nullable=True),
        sa.Column("predicate_type", sa.String, nullable=True),
        sa.Column("last_result", sa.Boolean, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("schedules")
    op.drop_table("monthly_costs")
    op.drop_table("rate_limits")
    op.drop_table("policy")
    op.drop_table("approvals")
    op.drop_table("tasks")
    op.drop_table("contacts")
