"""drop seats table and device seat id

Revision ID: 8b2d4f6e1a93
Revises: 4f6a2c8d1b90
Create Date: 2026-04-16 03:05:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "8b2d4f6e1a93"
down_revision: Union[str, Sequence[str], None] = "4f6a2c8d1b90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "devices" in tables and "seat_id" in _column_names(inspector, "devices"):
        op.drop_column("devices", "seat_id")

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "seats" in tables:
        op.drop_table("seats")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "seats" not in tables:
        op.create_table(
            "seats",
            sa.Column("id", sa.String(length=12), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("code", sa.String(length=100), nullable=True),
            sa.Column("staff_id", sa.String(length=12), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="idle"),
            sa.Column("note", sa.Text(), nullable=False, server_default=""),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_seats"),
        )

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "devices" in tables and "seat_id" not in _column_names(inspector, "devices"):
        op.add_column("devices", sa.Column("seat_id", sa.String(length=12), nullable=True))
