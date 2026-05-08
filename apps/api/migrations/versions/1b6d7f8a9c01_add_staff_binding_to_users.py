"""add staff binding to users

Revision ID: 1b6d7f8a9c01
Revises: 7c5f2b9f3a11
Create Date: 2026-03-23 22:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "1b6d7f8a9c01"
down_revision: Union[str, Sequence[str], None] = "7c5f2b9f3a11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "users" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("users")}
    if "staff_id" not in columns:
        op.add_column("users", sa.Column("staff_id", sa.String(length=12), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "users" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("users")}
    if "staff_id" in columns:
        op.drop_column("users", "staff_id")