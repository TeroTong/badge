"""add visit-order card recording auto-link fields

Revision ID: 20260510voar01
Revises: 20260509wic01
Create Date: 2026-05-10 00:25:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260510voar01"
down_revision = "20260509wic01"
branch_labels = None
depends_on = None


TABLE = "visit_order_advisor_notifications"


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


def _add_index(name: str, columns: list[str]) -> None:
    if name not in _indexes(TABLE):
        op.create_index(name, TABLE, columns)


def upgrade() -> None:
    columns = _columns(TABLE)
    if not columns:
        return

    additions = [
        ("recording_start_requested_at", sa.Column("recording_start_requested_at", sa.DateTime(timezone=True), nullable=True)),
        ("recording_start_staff_id", sa.Column("recording_start_staff_id", sa.String(length=12), nullable=True)),
        ("recording_start_device_id", sa.Column("recording_start_device_id", sa.String(length=100), nullable=True)),
        ("recording_start_device_code", sa.Column("recording_start_device_code", sa.String(length=100), nullable=True)),
        ("auto_link_status", sa.Column("auto_link_status", sa.String(length=30), nullable=True)),
        ("auto_link_recording_id", sa.Column("auto_link_recording_id", sa.String(length=12), nullable=True)),
        ("auto_linked_at", sa.Column("auto_linked_at", sa.DateTime(timezone=True), nullable=True)),
        ("auto_link_message_sent_at", sa.Column("auto_link_message_sent_at", sa.DateTime(timezone=True), nullable=True)),
        ("auto_link_error_message", sa.Column("auto_link_error_message", sa.Text(), nullable=True)),
    ]
    for name, column in additions:
        if name not in columns:
            op.add_column(TABLE, column)

    columns = _columns(TABLE)
    if "recording_start_requested_at" in columns:
        _add_index("ix_voan_start_at", ["recording_start_requested_at"])
    if "recording_start_staff_id" in columns:
        _add_index("ix_voan_start_staff", ["recording_start_staff_id"])
    if "recording_start_device_id" in columns:
        _add_index("ix_voan_start_device", ["recording_start_device_id"])
    if "auto_link_status" in columns:
        _add_index("ix_voan_auto_status", ["auto_link_status"])
    if "auto_link_recording_id" in columns:
        _add_index("ix_voan_auto_recording", ["auto_link_recording_id"])


def downgrade() -> None:
    for index_name in [
        "ix_voan_auto_recording",
        "ix_voan_auto_status",
        "ix_voan_start_device",
        "ix_voan_start_staff",
        "ix_voan_start_at",
    ]:
        if index_name in _indexes(TABLE):
            op.drop_index(index_name, table_name=TABLE)

    columns = _columns(TABLE)
    for column_name in [
        "auto_link_error_message",
        "auto_link_message_sent_at",
        "auto_linked_at",
        "auto_link_recording_id",
        "auto_link_status",
        "recording_start_device_code",
        "recording_start_device_id",
        "recording_start_staff_id",
        "recording_start_requested_at",
    ]:
        if column_name in columns:
            op.drop_column(TABLE, column_name)
