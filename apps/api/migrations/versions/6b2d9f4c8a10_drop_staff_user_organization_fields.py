"""drop staff and user organization fields

Revision ID: 6b2d9f4c8a10
Revises: f3b2c1d4e5a6
Create Date: 2026-04-29 17:30:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "6b2d9f4c8a10"
down_revision: Union[str, Sequence[str], None] = "f3b2c1d4e5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _index_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    for table_name in ("staff", "users"):
        if table_name not in tables:
            continue
        indexes = _index_names(inspector, table_name)
        index_name = f"ix_{table_name}_organization_code"
        if index_name in indexes:
            op.drop_index(index_name, table_name=table_name)

        inspector = sa.inspect(bind)
        columns = _column_names(inspector, table_name)
        if "organization_name" in columns:
            op.drop_column(table_name, "organization_name")
        if "organization_code" in columns:
            op.drop_column(table_name, "organization_code")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "staff" in tables:
        columns = _column_names(inspector, "staff")
        if "organization_code" not in columns:
            op.add_column("staff", sa.Column("organization_code", sa.String(length=20), nullable=True))
            op.create_index("ix_staff_organization_code", "staff", ["organization_code"], unique=False)
        if "organization_name" not in columns:
            op.add_column("staff", sa.Column("organization_name", sa.String(length=100), nullable=True))

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "users" in tables:
        columns = _column_names(inspector, "users")
        if "organization_code" not in columns:
            op.add_column("users", sa.Column("organization_code", sa.String(length=20), nullable=True))
            op.create_index("ix_users_organization_code", "users", ["organization_code"], unique=False)
        if "organization_name" not in columns:
            op.add_column("users", sa.Column("organization_name", sa.String(length=100), nullable=True))
