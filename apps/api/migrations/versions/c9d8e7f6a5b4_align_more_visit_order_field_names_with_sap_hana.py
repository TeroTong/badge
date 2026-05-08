"""align more visit_order field names with sap hana

Revision ID: c9d8e7f6a5b4
Revises: b6e7f8a9c0d1
Create Date: 2026-04-16 09:10:00.000000
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "c9d8e7f6a5b4"
down_revision = "b6e7f8a9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("visit_orders", "advxc_name", new_column_name="advxc_long")
    op.alter_column("visit_orders", "kf_id", new_column_name="vipkf")
    op.alter_column("visit_orders", "kf_id_dq", new_column_name="d_vipkf")


def downgrade() -> None:
    op.alter_column("visit_orders", "d_vipkf", new_column_name="kf_id_dq")
    op.alter_column("visit_orders", "vipkf", new_column_name="kf_id")
    op.alter_column("visit_orders", "advxc_long", new_column_name="advxc_name")
