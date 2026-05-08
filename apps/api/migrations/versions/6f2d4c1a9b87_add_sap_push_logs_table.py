"""add sap push logs table

Revision ID: 6f2d4c1a9b87
Revises: 4d7e6f5a9c21
Create Date: 2026-04-03 20:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "6f2d4c1a9b87"
down_revision = "4d7e6f5a9c21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "sap_push_logs" not in existing_tables:
        op.create_table(
            "sap_push_logs",
            sa.Column("id", sa.String(length=12), nullable=False),
            sa.Column("recording_id", sa.String(length=12), nullable=True),
            sa.Column("visit_id", sa.String(length=12), nullable=True),
            sa.Column("visit_order_no", sa.String(length=50), nullable=True),
            sa.Column("visit_order_seg", sa.String(length=20), nullable=True),
            sa.Column("customer_name", sa.String(length=100), nullable=True),
            sa.Column("customer_code", sa.String(length=100), nullable=True),
            sa.Column("advisor_name", sa.String(length=100), nullable=True),
            sa.Column("trigger_mode", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("send_enabled", sa.Boolean(), nullable=False),
            sa.Column("initiated_by", sa.String(length=100), nullable=True),
            sa.Column("request_url", sa.String(length=500), nullable=True),
            sa.Column("trace_id", sa.String(length=64), nullable=True),
            sa.Column("request_payloads", sa.JSON(), nullable=True),
            sa.Column("gateway_requests", sa.JSON(), nullable=True),
            sa.Column("response_items", sa.JSON(), nullable=True),
            sa.Column("http_status_code", sa.Integer(), nullable=True),
            sa.Column("business_status", sa.String(length=20), nullable=True),
            sa.Column("business_message", sa.Text(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["visit_id"], ["visits.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        existing_indexes: set[str] = set()
    else:
        existing_indexes = {index["name"] for index in inspector.get_indexes("sap_push_logs")}
    if "ix_sap_push_logs_recording_created_at" not in existing_indexes:
        op.create_index(
            "ix_sap_push_logs_recording_created_at",
            "sap_push_logs",
            ["recording_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if "sap_push_logs" not in existing_tables:
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes("sap_push_logs")}
    if "ix_sap_push_logs_recording_created_at" in existing_indexes:
        op.drop_index("ix_sap_push_logs_recording_created_at", table_name="sap_push_logs")

    op.drop_table("sap_push_logs")
