"""add device hospital scope

Revision ID: 6d8c0f2a9b31
Revises: a4f8c9d2e6b1
Create Date: 2026-04-30 14:20:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "6d8c0f2a9b31"
down_revision: Union[str, Sequence[str], None] = "a4f8c9d2e6b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _index_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "devices" not in inspector.get_table_names():
        return

    columns = _column_names(inspector, "devices")
    if "hospital_code" not in columns:
        op.add_column("devices", sa.Column("hospital_code", sa.String(length=100), nullable=True))
    if "hospital_short_name" not in columns:
        op.add_column("devices", sa.Column("hospital_short_name", sa.String(length=255), nullable=True))

    inspector = sa.inspect(bind)
    indexes = _index_names(inspector, "devices")
    if "ix_devices_hospital_code" not in indexes:
        op.create_index("ix_devices_hospital_code", "devices", ["hospital_code"], unique=False)

    devices = sa.table(
        "devices",
        sa.column("hospital_code", sa.String(length=100)),
        sa.column("hospital_short_name", sa.String(length=255)),
    )
    op.execute(
        sa.update(devices)
        .where(sa.or_(devices.c.hospital_code.is_(None), devices.c.hospital_code == ""))
        .values(hospital_code="6101", hospital_short_name="米兰柏羽总院")
    )
    op.execute(
        sa.update(devices)
        .where(
            sa.and_(
                devices.c.hospital_code == "6101",
                sa.or_(devices.c.hospital_short_name.is_(None), devices.c.hospital_short_name == ""),
            )
        )
        .values(hospital_short_name="米兰柏羽总院")
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "devices" not in inspector.get_table_names():
        return

    indexes = _index_names(inspector, "devices")
    if "ix_devices_hospital_code" in indexes:
        op.drop_index("ix_devices_hospital_code", table_name="devices")

    columns = _column_names(inspector, "devices")
    if "hospital_short_name" in columns:
        op.drop_column("devices", "hospital_short_name")
    if "hospital_code" in columns:
        op.drop_column("devices", "hospital_code")
