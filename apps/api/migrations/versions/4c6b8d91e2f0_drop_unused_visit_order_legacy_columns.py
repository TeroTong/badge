"""drop unused legacy columns from visit_orders

Revision ID: 4c6b8d91e2f0
Revises: 2a91f0c17be4
Create Date: 2026-04-16 06:10:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "4c6b8d91e2f0"
down_revision: Union[str, Sequence[str], None] = "2a91f0c17be4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("visit_orders", "yxpl_txt")
    op.drop_column("visit_orders", "ltext")
    op.drop_column("visit_orders", "remark_fz")
    op.drop_column("visit_orders", "kf_czbj")
    op.drop_column("visit_orders", "kf_czzzj")
    op.drop_column("visit_orders", "mzys2")
    op.drop_column("visit_orders", "mzys2_name")
    op.drop_column("visit_orders", "mzys3")
    op.drop_column("visit_orders", "mzys3_name")
    op.drop_column("visit_orders", "wz_doctor")
    op.drop_column("visit_orders", "wz_docname")
    op.drop_column("visit_orders", "pf_doctor")
    op.drop_column("visit_orders", "pf_docname")
    op.drop_column("visit_orders", "wk_doctor")
    op.drop_column("visit_orders", "wk_docname")


def downgrade() -> None:
    op.add_column("visit_orders", sa.Column("wk_docname", sa.String(length=50), nullable=True))
    op.add_column("visit_orders", sa.Column("wk_doctor", sa.String(length=20), nullable=True))
    op.add_column("visit_orders", sa.Column("pf_docname", sa.String(length=20), nullable=True))
    op.add_column("visit_orders", sa.Column("pf_doctor", sa.String(length=20), nullable=True))
    op.add_column("visit_orders", sa.Column("wz_docname", sa.String(length=20), nullable=True))
    op.add_column("visit_orders", sa.Column("wz_doctor", sa.String(length=20), nullable=True))
    op.add_column("visit_orders", sa.Column("mzys3_name", sa.String(length=50), nullable=True))
    op.add_column("visit_orders", sa.Column("mzys3", sa.String(length=36), nullable=True))
    op.add_column("visit_orders", sa.Column("mzys2_name", sa.String(length=50), nullable=True))
    op.add_column("visit_orders", sa.Column("mzys2", sa.String(length=36), nullable=True))
    op.add_column("visit_orders", sa.Column("kf_czzzj", sa.Numeric(precision=17, scale=4), nullable=True))
    op.add_column("visit_orders", sa.Column("kf_czbj", sa.Numeric(precision=17, scale=4), nullable=True))
    op.add_column("visit_orders", sa.Column("remark_fz", sa.String(length=765), nullable=True))
    op.add_column("visit_orders", sa.Column("ltext", sa.Text(), nullable=True))
    op.add_column("visit_orders", sa.Column("yxpl_txt", sa.String(length=180), nullable=True))
