"""add customer birthday retry fields

Revision ID: f3b2c1d4e5a6
Revises: e2f4a6c8b0d2
Create Date: 2026-04-27 18:55:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "f3b2c1d4e5a6"
down_revision = "e2f4a6c8b0d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "sap_hana_visit_orders" not in set(inspector.get_table_names()):
        return

    existing_columns = {column["name"] for column in inspector.get_columns("sap_hana_visit_orders")}
    if "customer_birthday" not in existing_columns:
        op.add_column("sap_hana_visit_orders", sa.Column("customer_birthday", sa.String(length=10), nullable=True))
    if "customer_birthday_lookup_at" not in existing_columns:
        op.add_column("sap_hana_visit_orders", sa.Column("customer_birthday_lookup_at", sa.DateTime(timezone=True), nullable=True))
    if "customer_birthday_retry_at" not in existing_columns:
        op.add_column("sap_hana_visit_orders", sa.Column("customer_birthday_retry_at", sa.DateTime(timezone=True), nullable=True))
    if "customer_birthday_retry_count" not in existing_columns:
        op.add_column(
            "sap_hana_visit_orders",
            sa.Column("customer_birthday_retry_count", sa.Integer(), nullable=False, server_default="0"),
        )

    existing_indexes = {index["name"] for index in inspector.get_indexes("sap_hana_visit_orders")}
    if "ix_sap_hana_visit_orders_customer_birthday_retry_at" not in existing_indexes:
        op.create_index(
            "ix_sap_hana_visit_orders_customer_birthday_retry_at",
            "sap_hana_visit_orders",
            ["customer_birthday_retry_at"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "sap_hana_visit_orders" not in set(inspector.get_table_names()):
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes("sap_hana_visit_orders")}
    if "ix_sap_hana_visit_orders_customer_birthday_retry_at" in existing_indexes:
        op.drop_index("ix_sap_hana_visit_orders_customer_birthday_retry_at", table_name="sap_hana_visit_orders")

    existing_columns = {column["name"] for column in inspector.get_columns("sap_hana_visit_orders")}
    for column_name in (
        "customer_birthday_retry_count",
        "customer_birthday_retry_at",
        "customer_birthday_lookup_at",
        "customer_birthday",
    ):
        if column_name in existing_columns:
            op.drop_column("sap_hana_visit_orders", column_name)
