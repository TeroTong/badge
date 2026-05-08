"""add visit_orders table

Revision ID: 3a7b9d2e4f16
Revises: 2f4c8f1e7a32
Create Date: 2026-03-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3a7b9d2e4f16'
down_revision: Union[str, None] = '2f4c8f1e7a32'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'visit_orders',
        sa.Column('id', sa.String(12), primary_key=True),
        sa.Column('dzdh', sa.String(30), nullable=False, index=True),
        sa.Column('dzseg', sa.String(9), nullable=True),
        sa.Column('sjrq', sa.String(10), nullable=True, index=True),
        sa.Column('yybm', sa.String(10), nullable=True, index=True),
        sa.Column('yyjc', sa.String(20), nullable=True),
        sa.Column('jgbm', sa.String(4), nullable=True),
        sa.Column('fzuer', sa.String(36), nullable=True, index=True),
        sa.Column('advxc', sa.String(36), nullable=True),
        sa.Column('advxc_name', sa.String(60), nullable=True),
        sa.Column('advyq', sa.String(36), nullable=True),
        sa.Column('advyq_name', sa.String(60), nullable=True),
        sa.Column('khbm', sa.String(50), nullable=True),
        sa.Column('khxm_jg', sa.String(100), nullable=True),
        sa.Column('khlx', sa.String(50), nullable=True),
        sa.Column('khlx_yg', sa.String(10), nullable=True),
        sa.Column('khlx_t30', sa.String(10), nullable=True),
        sa.Column('fzsj', sa.String(18), nullable=True),
        sa.Column('fzrq', sa.String(24), nullable=True),
        sa.Column('fzsta', sa.String(3), nullable=True),
        sa.Column('fzsta_txt', sa.String(180), nullable=True),
        sa.Column('jzsj', sa.String(18), nullable=True),
        sa.Column('jzrq', sa.String(24), nullable=True),
        sa.Column('jzks', sa.String(24), nullable=True),
        sa.Column('dztyp', sa.String(3), nullable=True),
        sa.Column('dztyp_txt', sa.String(180), nullable=True),
        sa.Column('dzsta', sa.String(3), nullable=True),
        sa.Column('dzsta_txt', sa.String(180), nullable=True),
        sa.Column('jcsta', sa.String(3), nullable=True),
        sa.Column('jcsta_txt', sa.String(180), nullable=True),
        sa.Column('csproj', sa.String(24), nullable=True),
        sa.Column('csproj_name', sa.String(120), nullable=True),
        sa.Column('mzys_name', sa.String(60), nullable=True),
        sa.Column('mzys', sa.String(36), nullable=True),
        sa.Column('qd1jfl', sa.String(10), nullable=True),
        sa.Column('qd2jfl', sa.String(100), nullable=True),
        sa.Column('z1jflt', sa.String(120), nullable=True),
        sa.Column('z2jflt', sa.String(120), nullable=True),
        sa.Column('z3jflt', sa.String(120), nullable=True),
        sa.Column('ltext', sa.Text, nullable=True),
        sa.Column('remark_dz', sa.String(765), nullable=True),
        sa.Column('remark_fz', sa.String(765), nullable=True),
        sa.Column('hylx_yg', sa.String(50), nullable=True),
        sa.Column('dymd_txt', sa.String(180), nullable=True),
        sa.Column('dzly_txt', sa.String(180), nullable=True),
        sa.Column('wk_je_all', sa.Numeric(17, 4), nullable=True),
        sa.Column('pf_je_all', sa.Numeric(17, 4), nullable=True),
        sa.Column('wc_je_all', sa.Numeric(17, 4), nullable=True),
        sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('dzdh', 'dzseg', name='uq_visit_orders_dzdh_dzseg'),
    )


def downgrade() -> None:
    op.drop_table('visit_orders')
