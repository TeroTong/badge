"""add hospital fields to staff

Revision ID: 2f4c8f1e7a32
Revises: 1b6d7f8a9c01
Create Date: 2026-03-23 23:30:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "2f4c8f1e7a32"
down_revision: Union[str, Sequence[str], None] = "1b6d7f8a9c01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "staff" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("staff")}
    if "hospital_code" not in columns:
        op.add_column("staff", sa.Column("hospital_code", sa.String(length=100), nullable=True))
    if "hospital_short_name" not in columns:
        op.add_column("staff", sa.Column("hospital_short_name", sa.String(length=255), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "staff" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("staff")}
    if "hospital_short_name" in columns:
        op.drop_column("staff", "hospital_short_name")
    if "hospital_code" in columns:
        op.drop_column("staff", "hospital_code")