"""add sap consultation review table

Revision ID: 20260509scr01
Revises: f9d3c2a1b0e8
Create Date: 2026-05-09 15:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260509scr01"
down_revision = "f9d3c2a1b0e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "sap_consultation_reviews" in tables:
        return

    op.create_table(
        "sap_consultation_reviews",
        sa.Column("id", sa.String(length=12), nullable=False),
        sa.Column("visit_id", sa.String(length=12), nullable=False),
        sa.Column("visit_order_no", sa.String(length=50), nullable=True),
        sa.Column("visit_order_seg", sa.String(length=20), nullable=True),
        sa.Column("hospital_code", sa.String(length=100), nullable=True),
        sa.Column("customer_name", sa.String(length=100), nullable=True),
        sa.Column("customer_code", sa.String(length=100), nullable=True),
        sa.Column("recording_ids", sa.JSON(), nullable=True),
        sa.Column("blocks", sa.JSON(), nullable=True),
        sa.Column("generated_text", sa.Text(), nullable=True),
        sa.Column("effective_text", sa.Text(), nullable=True),
        sa.Column("indication_payload", sa.JSON(), nullable=True),
        sa.Column("payload_snapshot", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("last_push_log_id", sa.String(length=12), nullable=True),
        sa.Column("created_by_staff_id", sa.String(length=12), nullable=True),
        sa.Column("updated_by_staff_id", sa.String(length=12), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by_staff_id"], ["staff.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["last_push_log_id"], ["sap_push_logs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_staff_id"], ["staff.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["visit_id"], ["visits.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("visit_id", name="uq_sap_consultation_reviews_visit"),
    )
    op.create_index("ix_sap_consultation_reviews_visit_id", "sap_consultation_reviews", ["visit_id"])
    op.create_index("ix_sap_consultation_reviews_visit_order_no", "sap_consultation_reviews", ["visit_order_no"])
    op.create_index("ix_sap_consultation_reviews_hospital_code", "sap_consultation_reviews", ["hospital_code"])
    op.create_index("ix_sap_consultation_reviews_status", "sap_consultation_reviews", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "sap_consultation_reviews" not in tables:
        return
    op.drop_index("ix_sap_consultation_reviews_status", table_name="sap_consultation_reviews")
    op.drop_index("ix_sap_consultation_reviews_hospital_code", table_name="sap_consultation_reviews")
    op.drop_index("ix_sap_consultation_reviews_visit_order_no", table_name="sap_consultation_reviews")
    op.drop_index("ix_sap_consultation_reviews_visit_id", table_name="sap_consultation_reviews")
    op.drop_table("sap_consultation_reviews")
