"""rename visit_order customer fields to sap names

Revision ID: b4f8c2d1e6a7
Revises: 9e1c7b3a4d52
Create Date: 2026-04-16 15:10:00.000000
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "b4f8c2d1e6a7"
down_revision = "9e1c7b3a4d52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("visit_orders", "khbm", new_column_name="kunr")
    op.alter_column("visit_orders", "khxm_jg", new_column_name="ninam")


def downgrade() -> None:
    op.alter_column("visit_orders", "ninam", new_column_name="khxm_jg")
    op.alter_column("visit_orders", "kunr", new_column_name="khbm")
