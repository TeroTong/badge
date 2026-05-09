"""add wecom interactive card callback fields

Revision ID: 20260509wic01
Revises: 20260509voan01
Create Date: 2026-05-09 23:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260509wic01"
down_revision = "20260509voan01"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    wecom_columns = _columns("wecom_tenants")
    if wecom_columns:
        if "callback_token" not in wecom_columns:
            op.add_column("wecom_tenants", sa.Column("callback_token", sa.String(length=255), nullable=True))
        if "callback_aes_key" not in wecom_columns:
            op.add_column("wecom_tenants", sa.Column("callback_aes_key", sa.String(length=255), nullable=True))

    notification_columns = _columns("visit_order_advisor_notifications")
    if notification_columns:
        if "wecom_task_id" not in notification_columns:
            op.add_column(
                "visit_order_advisor_notifications",
                sa.Column("wecom_task_id", sa.String(length=100), nullable=True),
            )
        if "wecom_response_code" not in notification_columns:
            op.add_column(
                "visit_order_advisor_notifications",
                sa.Column("wecom_response_code", sa.String(length=255), nullable=True),
            )
        if "ix_visit_order_advisor_notifications_wecom_task_id" not in _indexes(
            "visit_order_advisor_notifications"
        ):
            op.create_index(
                "ix_visit_order_advisor_notifications_wecom_task_id",
                "visit_order_advisor_notifications",
                ["wecom_task_id"],
            )


def downgrade() -> None:
    notification_columns = _columns("visit_order_advisor_notifications")
    if "wecom_task_id" in notification_columns:
        if "ix_visit_order_advisor_notifications_wecom_task_id" in _indexes(
            "visit_order_advisor_notifications"
        ):
            op.drop_index(
                "ix_visit_order_advisor_notifications_wecom_task_id",
                table_name="visit_order_advisor_notifications",
            )
        op.drop_column("visit_order_advisor_notifications", "wecom_task_id")
    if "wecom_response_code" in notification_columns:
        op.drop_column("visit_order_advisor_notifications", "wecom_response_code")

    wecom_columns = _columns("wecom_tenants")
    if "callback_token" in wecom_columns:
        op.drop_column("wecom_tenants", "callback_token")
    if "callback_aes_key" in wecom_columns:
        op.drop_column("wecom_tenants", "callback_aes_key")
