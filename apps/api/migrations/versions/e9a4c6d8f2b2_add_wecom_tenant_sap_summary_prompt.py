"""add wecom tenant sap summary prompt

Revision ID: e9a4c6d8f2b2
Revises: d2f6a8c9e1b4
Create Date: 2026-05-06 12:05:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e9a4c6d8f2b2"
down_revision: Union[str, Sequence[str], None] = "d2f6a8c9e1b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "wecom_tenants" not in set(inspector.get_table_names()):
        return

    columns = _column_names(inspector, "wecom_tenants")
    if "sap_summary_template_name" not in columns:
        op.add_column("wecom_tenants", sa.Column("sap_summary_template_name", sa.String(length=100), nullable=True))
    if "sap_summary_template_version" not in columns:
        op.add_column("wecom_tenants", sa.Column("sap_summary_template_version", sa.String(length=50), nullable=True))
    if "sap_summary_template" not in columns:
        op.add_column("wecom_tenants", sa.Column("sap_summary_template", sa.Text(), nullable=True))
    if "sap_summary_prompt" not in columns:
        op.add_column("wecom_tenants", sa.Column("sap_summary_prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "wecom_tenants" not in set(inspector.get_table_names()):
        return

    columns = _column_names(inspector, "wecom_tenants")
    if "sap_summary_prompt" in columns:
        op.drop_column("wecom_tenants", "sap_summary_prompt")
    if "sap_summary_template" in columns:
        op.drop_column("wecom_tenants", "sap_summary_template")
    if "sap_summary_template_version" in columns:
        op.drop_column("wecom_tenants", "sap_summary_template_version")
    if "sap_summary_template_name" in columns:
        op.drop_column("wecom_tenants", "sap_summary_template_name")
