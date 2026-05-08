"""align visit_order jgks and fzuer_long

Revision ID: b6e7f8a9c0d1
Revises: a7c9e1f2b3d4
Create Date: 2026-04-16 08:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b6e7f8a9c0d1"
down_revision = "a7c9e1f2b3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("visit_orders", sa.Column("fzuer_long", sa.String(length=100), nullable=True))
    op.alter_column(
        "visit_orders",
        "jgks_txt",
        new_column_name="jgks",
        existing_type=sa.String(length=180),
        existing_nullable=True,
    )
    op.execute(
        """
        update visit_orders as v
        set fzuer_long = coalesce(s.fzuer_long, v.fzr_name_dq)
        from sap_hana_visit_orders as s
        where v.dzdh = s.dzdh
          and coalesce(v.jgbm, '') = coalesce(s.jgbm, '')
          and v.fzuer_long is null
        """
    )
    op.execute(
        """
        update visit_orders
        set fzuer_long = fzr_name_dq
        where fzuer_long is null
          and fzr_name_dq is not null
        """
    )


def downgrade() -> None:
    op.alter_column(
        "visit_orders",
        "jgks",
        new_column_name="jgks_txt",
        existing_type=sa.String(length=180),
        existing_nullable=True,
    )
    op.drop_column("visit_orders", "fzuer_long")
