"""add dingtalk user id to staff

Revision ID: 8d4f2c1b7a90
Revises: 6f2d4c1a9b87
Create Date: 2026-04-09 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "8d4f2c1b7a90"
down_revision = "6f2d4c1a9b87"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    staff_columns = {column["name"] for column in inspector.get_columns("staff")}
    existing_indexes = {index["name"] for index in inspector.get_indexes("staff")}

    if "dingtalk_user_id" not in staff_columns:
        op.add_column("staff", sa.Column("dingtalk_user_id", sa.String(length=100), nullable=True))

    if "ix_staff_dingtalk_user_id" not in existing_indexes:
        op.create_index("ix_staff_dingtalk_user_id", "staff", ["dingtalk_user_id"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    staff_columns = {column["name"] for column in inspector.get_columns("staff")}
    existing_indexes = {index["name"] for index in inspector.get_indexes("staff")}

    if "ix_staff_dingtalk_user_id" in existing_indexes:
        op.drop_index("ix_staff_dingtalk_user_id", table_name="staff")

    if "dingtalk_user_id" in staff_columns:
        op.drop_column("staff", "dingtalk_user_id")
