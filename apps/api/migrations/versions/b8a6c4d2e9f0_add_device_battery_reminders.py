"""add device battery reminders

Revision ID: b8a6c4d2e9f0
Revises: 7a1c2d3e4f56
Create Date: 2026-04-30 21:30:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b8a6c4d2e9f0"
down_revision: Union[str, Sequence[str], None] = "7a1c2d3e4f56"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "device_battery_reminders" in inspector.get_table_names():
        return

    op.create_table(
        "device_battery_reminders",
        sa.Column("id", sa.String(length=12), nullable=False),
        sa.Column("device_id", sa.String(length=12), nullable=True),
        sa.Column("device_code", sa.String(length=100), nullable=False),
        sa.Column("staff_id", sa.String(length=12), nullable=True),
        sa.Column("wecom_user_id", sa.String(length=100), nullable=True),
        sa.Column("wecom_corp_id", sa.String(length=100), nullable=True),
        sa.Column("last_battery_level", sa.Integer(), nullable=True),
        sa.Column("alert_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notified_date", sa.String(length=10), nullable=True),
        sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["staff_id"], ["staff.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_code", "staff_id", name="uq_device_battery_reminders_device_staff"),
    )
    op.create_index("ix_device_battery_reminders_device_id", "device_battery_reminders", ["device_id"], unique=False)
    op.create_index("ix_device_battery_reminders_device_code", "device_battery_reminders", ["device_code"], unique=False)
    op.create_index("ix_device_battery_reminders_staff_id", "device_battery_reminders", ["staff_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "device_battery_reminders" not in inspector.get_table_names():
        return

    op.drop_index("ix_device_battery_reminders_staff_id", table_name="device_battery_reminders")
    op.drop_index("ix_device_battery_reminders_device_code", table_name="device_battery_reminders")
    op.drop_index("ix_device_battery_reminders_device_id", table_name="device_battery_reminders")
    op.drop_table("device_battery_reminders")
