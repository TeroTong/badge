"""drop more visit_order compatibility columns

Revision ID: f1d2c3b4a5e6
Revises: b4f8c2d1e6a7
Create Date: 2026-04-16 15:45:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f1d2c3b4a5e6"
down_revision = "b4f8c2d1e6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("visit_orders", "yybm")
    op.drop_column("visit_orders", "yyjc")
    op.drop_column("visit_orders", "advyq_dq")
    op.drop_column("visit_orders", "advyq_dq_name")
    op.drop_column("visit_orders", "mdfdt")
    op.drop_column("visit_orders", "mdftm")
    op.drop_column("visit_orders", "synced_at")


def downgrade() -> None:
    op.add_column("visit_orders", sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("visit_orders", sa.Column("mdftm", sa.String(length=8), nullable=True))
    op.add_column("visit_orders", sa.Column("mdfdt", sa.String(length=10), nullable=True))
    op.add_column("visit_orders", sa.Column("advyq_dq_name", sa.String(length=50), nullable=True))
    op.add_column("visit_orders", sa.Column("advyq_dq", sa.String(length=100), nullable=True))
    op.add_column("visit_orders", sa.Column("yyjc", sa.String(length=20), nullable=True))
    op.add_column("visit_orders", sa.Column("yybm", sa.String(length=10), nullable=True))
    op.create_index(op.f("ix_visit_orders_yybm"), "visit_orders", ["yybm"], unique=False)
