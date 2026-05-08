"""add risk rules and records

Revision ID: b7f4d3e2c1a0
Revises: a6e9d5f7c4b2
Create Date: 2026-03-21 16:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b7f4d3e2c1a0"
down_revision = "a6e9d5f7c4b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "risk_rules",
        sa.Column("id", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("match_type", sa.String(length=50), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False, server_default="medium"),
        sa.Column("risk_label", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("match_config", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "risk_records",
        sa.Column("id", sa.String(length=12), nullable=False),
        sa.Column("rule_id", sa.String(length=12), nullable=True),
        sa.Column("task_id", sa.String(length=12), nullable=False),
        sa.Column("recording_id", sa.String(length=12), nullable=True),
        sa.Column("visit_id", sa.String(length=12), nullable=True),
        sa.Column("customer_id", sa.String(length=12), nullable=True),
        sa.Column("staff_id", sa.String(length=12), nullable=True),
        sa.Column("source_type", sa.String(length=20), nullable=False, server_default="recording"),
        sa.Column("rule_name", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("risk_label", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("severity", sa.String(length=20), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("matched_dimension_name", sa.String(length=100), nullable=True),
        sa.Column("matched_keywords", sa.JSON(), nullable=False),
        sa.Column("overall_score", sa.Float(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("hit_excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["rule_id"], ["risk_rules.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["staff_id"], ["staff.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["analysis_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["visit_id"], ["visits.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "task_id", name="uq_risk_records_rule_task"),
    )

    op.create_index("ix_risk_records_created_at", "risk_records", ["created_at"], unique=False)
    op.create_index("ix_risk_records_staff_id", "risk_records", ["staff_id"], unique=False)
    op.create_index("ix_risk_records_status", "risk_records", ["status"], unique=False)
    op.create_index("ix_risk_records_severity", "risk_records", ["severity"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_risk_records_severity", table_name="risk_records")
    op.drop_index("ix_risk_records_status", table_name="risk_records")
    op.drop_index("ix_risk_records_staff_id", table_name="risk_records")
    op.drop_index("ix_risk_records_created_at", table_name="risk_records")
    op.drop_table("risk_records")
    op.drop_table("risk_rules")
