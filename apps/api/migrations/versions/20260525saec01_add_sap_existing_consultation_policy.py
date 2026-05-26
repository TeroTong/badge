"""add institution sap existing consultation policy

Revision ID: 20260525saec01
Revises: 20260510voar01
Create Date: 2026-05-25 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260525saec01"
down_revision = "20260510voar01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("wecom_tenants")}
    if "sap_auto_update_existing_consultation" not in columns:
        op.add_column(
            "wecom_tenants",
            sa.Column(
                "sap_auto_update_existing_consultation",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    op.execute(
        """
        UPDATE wecom_tenants
        SET sap_auto_update_existing_consultation = TRUE
        WHERE default_hospital_code = '6101'
        """
    )
    op.execute(
        """
        UPDATE wecom_tenants
        SET sap_auto_update_existing_consultation = FALSE
        WHERE default_hospital_code = '6501'
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("wecom_tenants")}
    if "sap_auto_update_existing_consultation" in columns:
        op.drop_column("wecom_tenants", "sap_auto_update_existing_consultation")
