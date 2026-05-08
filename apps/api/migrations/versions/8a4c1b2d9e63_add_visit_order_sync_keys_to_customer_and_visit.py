"""add visit-order sync keys to customer and visit

Revision ID: 8a4c1b2d9e63
Revises: 7d3f1a2e9b04
Create Date: 2026-03-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8a4c1b2d9e63"
down_revision: str = "7d3f1a2e9b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("customers") as batch_op:
        batch_op.add_column(sa.Column("external_customer_code", sa.String(length=50), nullable=True))
        batch_op.create_index("ix_customers_external_customer_code", ["external_customer_code"], unique=False)
        batch_op.create_unique_constraint("uq_customers_external_customer_code", ["external_customer_code"])

    with op.batch_alter_table("visits") as batch_op:
        batch_op.add_column(sa.Column("external_visit_order_no", sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column("external_visit_order_seg", sa.String(length=9), nullable=True))
        batch_op.create_index("ix_visits_external_visit_order_no", ["external_visit_order_no"], unique=False)
        batch_op.create_unique_constraint(
            "uq_visits_visit_order_ref",
            ["external_visit_order_no", "external_visit_order_seg"],
        )


def downgrade() -> None:
    with op.batch_alter_table("visits") as batch_op:
        batch_op.drop_constraint("uq_visits_visit_order_ref", type_="unique")
        batch_op.drop_index("ix_visits_external_visit_order_no")
        batch_op.drop_column("external_visit_order_seg")
        batch_op.drop_column("external_visit_order_no")

    with op.batch_alter_table("customers") as batch_op:
        batch_op.drop_constraint("uq_customers_external_customer_code", type_="unique")
        batch_op.drop_index("ix_customers_external_customer_code")
        batch_op.drop_column("external_customer_code")
