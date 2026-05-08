"""drop duplicate visit_order jgks columns

Revision ID: a7c9e1f2b3d4
Revises: f1d2c3b4a5e6
Create Date: 2026-04-16 08:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a7c9e1f2b3d4"
down_revision = "f1d2c3b4a5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("visit_orders", "jzks")
    op.drop_column("visit_orders", "z1jflt")


def downgrade() -> None:
    op.add_column("visit_orders", sa.Column("z1jflt", sa.String(length=120), nullable=True))
    op.add_column("visit_orders", sa.Column("jzks", sa.String(length=24), nullable=True))
