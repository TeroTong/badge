"""add visit order advisor notification table

Revision ID: 20260509voan01
Revises: 20260509scr01
Create Date: 2026-05-09 22:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260509voan01"
down_revision = "20260509scr01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "visit_order_advisor_notifications" in tables:
        return

    op.create_table(
        "visit_order_advisor_notifications",
        sa.Column("id", sa.String(length=12), nullable=False),
        sa.Column("hospital_code", sa.String(length=20), nullable=False),
        sa.Column("visit_order_no", sa.String(length=30), nullable=False),
        sa.Column("visit_order_seg", sa.String(length=9), nullable=False),
        sa.Column("triage_no", sa.String(length=50), nullable=True),
        sa.Column("advisor_code", sa.String(length=36), nullable=True),
        sa.Column("advisor_name", sa.String(length=100), nullable=True),
        sa.Column("advisor_staff_id", sa.String(length=12), nullable=True),
        sa.Column("wecom_user_id", sa.String(length=100), nullable=True),
        sa.Column("customer_code", sa.String(length=50), nullable=True),
        sa.Column("customer_name", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["advisor_staff_id"], ["staff.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "hospital_code",
            "visit_order_no",
            "visit_order_seg",
            "advisor_staff_id",
            name="uq_visit_order_advisor_notifications_target",
        ),
    )
    op.create_index(
        "ix_visit_order_advisor_notifications_hospital_code",
        "visit_order_advisor_notifications",
        ["hospital_code"],
    )
    op.create_index(
        "ix_visit_order_advisor_notifications_visit_order_no",
        "visit_order_advisor_notifications",
        ["visit_order_no"],
    )
    op.create_index(
        "ix_visit_order_advisor_notifications_advisor_code",
        "visit_order_advisor_notifications",
        ["advisor_code"],
    )
    op.create_index(
        "ix_visit_order_advisor_notifications_advisor_staff_id",
        "visit_order_advisor_notifications",
        ["advisor_staff_id"],
    )
    op.create_index(
        "ix_visit_order_advisor_notifications_status",
        "visit_order_advisor_notifications",
        ["status"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "visit_order_advisor_notifications" not in tables:
        return
    op.drop_index("ix_visit_order_advisor_notifications_status", table_name="visit_order_advisor_notifications")
    op.drop_index("ix_visit_order_advisor_notifications_advisor_staff_id", table_name="visit_order_advisor_notifications")
    op.drop_index("ix_visit_order_advisor_notifications_advisor_code", table_name="visit_order_advisor_notifications")
    op.drop_index("ix_visit_order_advisor_notifications_visit_order_no", table_name="visit_order_advisor_notifications")
    op.drop_index("ix_visit_order_advisor_notifications_hospital_code", table_name="visit_order_advisor_notifications")
    op.drop_table("visit_order_advisor_notifications")
