"""add missing sap hana fields to visit_orders

Revision ID: d1e2f3a4b5c6
Revises: c9d8e7f6a5b4
Create Date: 2026-04-16 09:25:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d1e2f3a4b5c6"
down_revision = "c9d8e7f6a5b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("visit_orders", sa.Column("kusex", sa.String(length=20), nullable=True))
    op.add_column("visit_orders", sa.Column("kutyp_dq", sa.String(length=50), nullable=True))
    op.add_column("visit_orders", sa.Column("kut30_dq", sa.String(length=10), nullable=True))
    op.add_column("visit_orders", sa.Column("kusta_dq", sa.String(length=50), nullable=True))
    op.add_column("visit_orders", sa.Column("d_fzuer", sa.String(length=36), nullable=True))
    op.add_column("visit_orders", sa.Column("dzly", sa.String(length=20), nullable=True))
    op.add_column("visit_orders", sa.Column("dymd", sa.String(length=20), nullable=True))
    op.add_column("visit_orders", sa.Column("kusrc", sa.String(length=100), nullable=True))
    op.add_column("visit_orders", sa.Column("kusrc2", sa.String(length=100), nullable=True))
    op.add_column("visit_orders", sa.Column("bjzx", sa.String(length=20), nullable=True))

    op.execute(
        """
        UPDATE visit_orders AS vo
        SET
            kusex = sho.kusex,
            kutyp_dq = sho.kutyp_dq,
            kut30_dq = sho.kut30_dq,
            kusta_dq = sho.kusta_dq,
            d_fzuer = sho.d_fzuer,
            dzly = sho.dzly,
            dymd = sho.dymd,
            kusrc = sho.kusrc,
            kusrc2 = sho.kusrc2,
            bjzx = sho.bjzx
        FROM sap_hana_visit_orders AS sho
        WHERE vo.jgbm = sho.jgbm
          AND vo.dzdh = sho.dzdh
        """
    )

    op.execute(
        """
        UPDATE visit_orders
        SET
            kusex = COALESCE(
                kusex,
                CASE customer_gender
                    WHEN '男' THEN 'M'
                    WHEN '女' THEN 'F'
                    ELSE NULL
                END
            ),
            kutyp_dq = COALESCE(kutyp_dq, khlx),
            kut30_dq = COALESCE(kut30_dq, khlx_t30),
            kusta_dq = COALESCE(kusta_dq, khlx2),
            d_fzuer = COALESCE(d_fzuer, fzr_id_dq),
            dzly = COALESCE(
                dzly,
                CASE dzly_txt
                    WHEN '已预约' THEN 'Y'
                    WHEN '未预约' THEN 'N'
                    ELSE NULL
                END
            ),
            dymd = COALESCE(
                dymd,
                CASE dymd_txt
                    WHEN '咨询' THEN 'A'
                    WHEN '治疗' THEN 'B'
                    WHEN '手术' THEN 'C'
                    WHEN '复查' THEN 'D'
                    WHEN '未到院购买' THEN 'X'
                    WHEN '其他' THEN 'Z'
                    ELSE NULL
                END
            ),
            kusrc = COALESCE(kusrc, qdly1_txt),
            kusrc2 = COALESCE(kusrc2, qdly2_txt)
        """
    )


def downgrade() -> None:
    op.drop_column("visit_orders", "bjzx")
    op.drop_column("visit_orders", "kusrc2")
    op.drop_column("visit_orders", "kusrc")
    op.drop_column("visit_orders", "dymd")
    op.drop_column("visit_orders", "dzly")
    op.drop_column("visit_orders", "d_fzuer")
    op.drop_column("visit_orders", "kusta_dq")
    op.drop_column("visit_orders", "kut30_dq")
    op.drop_column("visit_orders", "kutyp_dq")
    op.drop_column("visit_orders", "kusex")
