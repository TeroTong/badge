"""add visit_order extra columns

Revision ID: 5c8e2a1b3d47
Revises: 3a7b9d2e4f16
Create Date: 2026-03-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "5c8e2a1b3d47"
down_revision: Union[str, None] = "3a7b9d2e4f16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_COLUMNS = [
    ("yydh", sa.String(30)),
    ("yyuer", sa.String(36)),
    ("khlx2", sa.String(50)),
    ("kulvl_dq", sa.String(20)),
    ("kf_id", sa.String(36)),
    ("fzr_id_dq", sa.String(36)),
    ("kf_id_dq", sa.String(36)),
    ("fzdh", sa.String(30)),
    ("fzrid", sa.String(36)),
    ("ddsc", sa.String(30)),
    ("bhkx", sa.String(3)),
    ("assxc", sa.String(36)),
    ("jgks_txt", sa.String(180)),
    ("xgyy", sa.String(60)),
    ("yxpl_txt", sa.String(180)),
    ("syz_txt", sa.String(180)),
    ("cl_txt", sa.String(180)),
    ("qdly1_txt", sa.String(180)),
    ("qdly2_txt", sa.String(180)),
    ("crtdt", sa.String(10)),
    ("crttm", sa.String(8)),
    ("mdfdt", sa.String(10)),
    ("mdftm", sa.String(8)),
]


def upgrade() -> None:
    for col_name, col_type in _NEW_COLUMNS:
        op.add_column("visit_orders", sa.Column(col_name, col_type, nullable=True))


def downgrade() -> None:
    for col_name, _ in reversed(_NEW_COLUMNS):
        op.drop_column("visit_orders", col_name)
