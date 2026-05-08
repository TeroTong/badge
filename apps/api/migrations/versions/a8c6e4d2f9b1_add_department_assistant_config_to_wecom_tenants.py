"""add department assistant config to wecom tenants

Revision ID: a8c6e4d2f9b1
Revises: f6b8c9d0e1a2
Create Date: 2026-05-06 16:45:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a8c6e4d2f9b1"
down_revision = "f6b8c9d0e1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "wecom_tenants" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("wecom_tenants")}
    if "department_assistant_match_config" not in columns:
        op.add_column(
            "wecom_tenants",
            sa.Column("department_assistant_match_config", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "wecom_tenants" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("wecom_tenants")}
    if "department_assistant_match_config" in columns:
        op.drop_column("wecom_tenants", "department_assistant_match_config")
