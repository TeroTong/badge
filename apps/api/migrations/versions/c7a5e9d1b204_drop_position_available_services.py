"""drop unused position available_services column

Revision ID: c7a5e9d1b204
Revises: 9a1f2b3c4d5e
Create Date: 2026-04-04 23:25:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c7a5e9d1b204"
down_revision: Union[str, Sequence[str], None] = "9a1f2b3c4d5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "position_profiles" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("position_profiles")}
    if "available_services" in columns:
        op.drop_column("position_profiles", "available_services")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "position_profiles" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("position_profiles")}
    if "available_services" not in columns:
        op.add_column(
            "position_profiles",
            sa.Column("available_services", sa.JSON(), nullable=False, server_default="[]"),
        )
