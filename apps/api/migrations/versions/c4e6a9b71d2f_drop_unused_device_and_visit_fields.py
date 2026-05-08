"""drop unused device and visit fields

Revision ID: c4e6a9b71d2f
Revises: 8b2d4f6e1a93
Create Date: 2026-04-16 03:32:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c4e6a9b71d2f"
down_revision: Union[str, Sequence[str], None] = "8b2d4f6e1a93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "devices" in tables:
        device_columns = _column_names(inspector, "devices")
        if "gps_location" in device_columns:
            op.drop_column("devices", "gps_location")
        if "last_seen_ip" in device_columns:
            op.drop_column("devices", "last_seen_ip")
        if "last_heartbeat_at" in device_columns:
            op.drop_column("devices", "last_heartbeat_at")

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "visits" in tables and "intention_level" in _column_names(inspector, "visits"):
        op.drop_column("visits", "intention_level")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "devices" in tables:
        device_columns = _column_names(inspector, "devices")
        if "gps_location" not in device_columns:
            op.add_column("devices", sa.Column("gps_location", sa.String(length=255), nullable=True))
        if "last_seen_ip" not in device_columns:
            op.add_column("devices", sa.Column("last_seen_ip", sa.String(length=64), nullable=True))
        if "last_heartbeat_at" not in device_columns:
            op.add_column("devices", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "visits" in tables and "intention_level" not in _column_names(inspector, "visits"):
        op.add_column("visits", sa.Column("intention_level", sa.String(length=20), nullable=True))
