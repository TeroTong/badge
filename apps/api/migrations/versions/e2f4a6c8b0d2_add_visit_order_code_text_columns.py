"""add visit order code text columns

Revision ID: e2f4a6c8b0d2
Revises: d1e2f3a4b5c6
Create Date: 2026-04-16 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e2f4a6c8b0d2"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("visit_orders") as batch_op:
        batch_op.add_column(sa.Column("kusex_txt", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("kutyp_dq_txt", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("kut30_dq_txt", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("kusta_dq_txt", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("jgks_txt", sa.String(length=180), nullable=True))

    op.execute(
        """
        UPDATE visit_orders
        SET
            kusex_txt = CASE btrim(coalesce(kusex, ''))
                WHEN 'M' THEN '男'
                WHEN 'F' THEN '女'
                WHEN '男' THEN '男'
                WHEN '女' THEN '女'
                ELSE NULL
            END,
            kutyp_dq_txt = CASE btrim(coalesce(kutyp_dq, ''))
                WHEN 'Q' THEN '潜客/新客'
                WHEN 'V' THEN '会员/老客'
                WHEN '潜客/新客' THEN '潜客/新客'
                WHEN '会员/老客' THEN '会员/老客'
                ELSE NULLIF(btrim(coalesce(khlx, '')), '')
            END,
            kut30_dq_txt = CASE btrim(coalesce(kut30_dq, ''))
                WHEN 'Q' THEN '潜客/新客'
                WHEN 'V' THEN '会员/老客'
                WHEN '潜客/新客' THEN '潜客/新客'
                WHEN '会员/老客' THEN '会员/老客'
                ELSE NULLIF(btrim(coalesce(khlx_t30, '')), '')
            END,
            kusta_dq_txt = CASE btrim(coalesce(kusta_dq, ''))
                WHEN 'Q1' THEN '建档未上门'
                WHEN 'Q2' THEN '上门未成交'
                WHEN 'Q3' THEN '体验会员'
                WHEN 'V1' THEN '付费会员'
                WHEN '建档未上门' THEN '建档未上门'
                WHEN '上门未成交' THEN '上门未成交'
                WHEN '体验会员' THEN '体验会员'
                WHEN '付费会员' THEN '付费会员'
                ELSE NULLIF(btrim(coalesce(khlx2, '')), '')
            END,
            jgks_txt = CASE btrim(coalesce(jgks, ''))
                WHEN 'JGKS01' THEN '口腔科'
                WHEN 'JGKS02' THEN '皮肤科'
                WHEN 'JGKS03' THEN '外科'
                WHEN 'JGKS04' THEN '微整科'
                WHEN 'JGKS05' THEN '中医'
                WHEN 'JGKS06' THEN '纹绣'
                WHEN 'JGKS07' THEN '会籍'
                WHEN 'JGKS08' THEN '毛发移植科'
                WHEN 'JGKS09' THEN '非手术'
                WHEN 'JGKS10' THEN '私密中心'
                WHEN 'JGKS11' THEN '纤体中心'
                WHEN 'JGKS12' THEN '植发中心'
                WHEN 'JGKS13' THEN '形体私密中心'
                WHEN 'JGKS14' THEN 'SPA中心'
                ELSE NULLIF(btrim(coalesce(jgks, '')), '')
            END
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("visit_orders") as batch_op:
        batch_op.drop_column("jgks_txt")
        batch_op.drop_column("kusta_dq_txt")
        batch_op.drop_column("kut30_dq_txt")
        batch_op.drop_column("kutyp_dq_txt")
        batch_op.drop_column("kusex_txt")
