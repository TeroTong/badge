"""add sap push message notification fields

Revision ID: f6b8c9d0e1a2
Revises: e9a4c6d8f2b2
Create Date: 2026-05-06 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f6b8c9d0e1a2"
down_revision = "e9a4c6d8f2b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sap_push_logs",
        sa.Column("message_success_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "sap_push_logs",
        sa.Column("message_failure_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("sap_push_logs", sa.Column("message_notify_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sap_push_logs", "message_notify_error")
    op.drop_column("sap_push_logs", "message_failure_notified_at")
    op.drop_column("sap_push_logs", "message_success_notified_at")
