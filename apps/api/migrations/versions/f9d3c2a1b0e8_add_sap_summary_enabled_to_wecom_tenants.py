"""add sap summary enabled flag to wecom tenants

Revision ID: f9d3c2a1b0e8
Revises: a7c9e1f2b4d6
Create Date: 2026-05-09 12:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "f9d3c2a1b0e8"
down_revision = "a7c9e1f2b4d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("wecom_tenants")}
    if "sap_summary_enabled" not in columns:
        op.add_column(
            "wecom_tenants",
            sa.Column("sap_summary_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("wecom_tenants")}
    if "sap_summary_enabled" in columns:
        op.drop_column("wecom_tenants", "sap_summary_enabled")
