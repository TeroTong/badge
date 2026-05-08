"""add devices table

Revision ID: a6e9d5f7c4b2
Revises: e4b7f3c2a981
Create Date: 2026-03-21 10:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "a6e9d5f7c4b2"
down_revision = "e4b7f3c2a981"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "devices" in inspector.get_table_names():
        return

    op.create_table(
        "devices",
        sa.Column("id", sa.String(length=12), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("device_code", sa.String(length=100), nullable=False),
        sa.Column("team_id", sa.String(length=12), nullable=True),
        sa.Column("seat_id", sa.String(length=12), nullable=True),
        sa.Column("staff_id", sa.String(length=12), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="offline"),
        sa.Column("battery_level", sa.Integer(), nullable=True),
        sa.Column("gps_location", sa.String(length=255), nullable=True),
        sa.Column("last_seen_ip", sa.String(length=64), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("device_code", name=op.f("uq_devices_device_code")),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "devices" in inspector.get_table_names():
        op.drop_table("devices")
