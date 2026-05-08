"""add dingtalk binding cache to devices

Revision ID: d2f6a8c9e1b4
Revises: c1d2e3f4a5b6
Create Date: 2026-05-05 17:10:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d2f6a8c9e1b4"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "devices" not in set(inspector.get_table_names()):
        return

    columns = _column_names(inspector, "devices")
    if "dingtalk_team_code" not in columns:
        op.add_column("devices", sa.Column("dingtalk_team_code", sa.String(length=100), nullable=True))
    if "dingtalk_user_id" not in columns:
        op.add_column("devices", sa.Column("dingtalk_user_id", sa.String(length=100), nullable=True))
    if "dingtalk_binding_synced_at" not in columns:
        op.add_column("devices", sa.Column("dingtalk_binding_synced_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "devices" not in set(inspector.get_table_names()):
        return

    columns = _column_names(inspector, "devices")
    if "dingtalk_binding_synced_at" in columns:
        op.drop_column("devices", "dingtalk_binding_synced_at")
    if "dingtalk_user_id" in columns:
        op.drop_column("devices", "dingtalk_user_id")
    if "dingtalk_team_code" in columns:
        op.drop_column("devices", "dingtalk_team_code")
