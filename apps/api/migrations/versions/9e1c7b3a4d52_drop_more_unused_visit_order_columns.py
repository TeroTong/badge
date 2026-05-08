"""drop more unused visit_order columns now absent from sap hana

Revision ID: 9e1c7b3a4d52
Revises: 4c6b8d91e2f0
Create Date: 2026-04-16 08:05:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "9e1c7b3a4d52"
down_revision: Union[str, Sequence[str], None] = "4c6b8d91e2f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("visit_orders", "csproj")
    op.drop_column("visit_orders", "csproj_name")
    op.drop_column("visit_orders", "syz_txt")
    op.drop_column("visit_orders", "cl_txt")
    op.drop_column("visit_orders", "mzys_name")
    op.drop_column("visit_orders", "mzys")
    op.drop_column("visit_orders", "z2jflt")
    op.drop_column("visit_orders", "z3jflt")


def downgrade() -> None:
    op.add_column("visit_orders", sa.Column("z3jflt", sa.String(length=120), nullable=True))
    op.add_column("visit_orders", sa.Column("z2jflt", sa.String(length=120), nullable=True))
    op.add_column("visit_orders", sa.Column("mzys", sa.String(length=36), nullable=True))
    op.add_column("visit_orders", sa.Column("mzys_name", sa.String(length=60), nullable=True))
    op.add_column("visit_orders", sa.Column("cl_txt", sa.String(length=180), nullable=True))
    op.add_column("visit_orders", sa.Column("syz_txt", sa.String(length=180), nullable=True))
    op.add_column("visit_orders", sa.Column("csproj_name", sa.String(length=120), nullable=True))
    op.add_column("visit_orders", sa.Column("csproj", sa.String(length=24), nullable=True))
