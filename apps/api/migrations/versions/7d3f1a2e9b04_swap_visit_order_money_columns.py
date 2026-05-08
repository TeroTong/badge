"""swap visit_order money columns: drop xgyy/wk_je_all/pf_je_all/wc_je_all, add kf_czbj/kf_czzzj

Revision ID: 7d3f1a2e9b04
Revises: 5c8e2a1b3d47
Create Date: 2026-03-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "7d3f1a2e9b04"
down_revision: str = "5c8e2a1b3d47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite doesn't support DROP COLUMN before 3.35; use batch mode
    with op.batch_alter_table("visit_orders") as batch_op:
        batch_op.drop_column("xgyy")
        batch_op.drop_column("wk_je_all")
        batch_op.drop_column("pf_je_all")
        batch_op.drop_column("wc_je_all")
        batch_op.add_column(sa.Column("kf_czbj", sa.Numeric(17, 4), nullable=True))
        batch_op.add_column(sa.Column("kf_czzzj", sa.Numeric(17, 4), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("visit_orders") as batch_op:
        batch_op.drop_column("kf_czzzj")
        batch_op.drop_column("kf_czbj")
        batch_op.add_column(sa.Column("wc_je_all", sa.Numeric(17, 4), nullable=True))
        batch_op.add_column(sa.Column("pf_je_all", sa.Numeric(17, 4), nullable=True))
        batch_op.add_column(sa.Column("wk_je_all", sa.Numeric(17, 4), nullable=True))
        batch_op.add_column(sa.Column("xgyy", sa.String(60), nullable=True))
