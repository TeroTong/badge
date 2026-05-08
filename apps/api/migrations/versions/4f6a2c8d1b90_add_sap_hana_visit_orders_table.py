"""add sap hana visit orders table

Revision ID: 4f6a2c8d1b90
Revises: 3e1d5a7b9c24
Create Date: 2026-04-15 11:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "4f6a2c8d1b90"
down_revision = "3e1d5a7b9c24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "sap_hana_visit_orders" not in existing_tables:
        op.create_table(
            "sap_hana_visit_orders",
            sa.Column("id", sa.String(length=12), nullable=False),
            sa.Column("jgbm", sa.String(length=20), nullable=False),
            sa.Column("dzdh", sa.String(length=30), nullable=False),
            sa.Column("yydh", sa.String(length=30), nullable=True),
            sa.Column("crtdt", sa.String(length=20), nullable=True),
            sa.Column("crttm", sa.String(length=20), nullable=True),
            sa.Column("dzsta", sa.String(length=10), nullable=True),
            sa.Column("kunr", sa.String(length=50), nullable=True),
            sa.Column("ninam", sa.String(length=100), nullable=True),
            sa.Column("kusex", sa.String(length=20), nullable=True),
            sa.Column("kulvl_dq", sa.String(length=50), nullable=True),
            sa.Column("kutyp_dq", sa.String(length=50), nullable=True),
            sa.Column("kut30_dq", sa.String(length=50), nullable=True),
            sa.Column("kusta_dq", sa.String(length=50), nullable=True),
            sa.Column("dzly", sa.String(length=20), nullable=True),
            sa.Column("dymd", sa.String(length=20), nullable=True),
            sa.Column("dztyp", sa.String(length=20), nullable=True),
            sa.Column("remark_dz", sa.Text(), nullable=True),
            sa.Column("jgks", sa.String(length=100), nullable=True),
            sa.Column("fzuer", sa.String(length=36), nullable=True),
            sa.Column("fzuer_long", sa.String(length=100), nullable=True),
            sa.Column("vipkf", sa.String(length=36), nullable=True),
            sa.Column("d_fzuer", sa.String(length=36), nullable=True),
            sa.Column("d_vipkf", sa.String(length=36), nullable=True),
            sa.Column("advyq", sa.String(length=36), nullable=True),
            sa.Column("kusrc", sa.String(length=100), nullable=True),
            sa.Column("kusrc2", sa.String(length=100), nullable=True),
            sa.Column("yyuer", sa.String(length=36), nullable=True),
            sa.Column("bjzx", sa.String(length=20), nullable=True),
            sa.Column("bhkx", sa.String(length=20), nullable=True),
            sa.Column("fzdata", sa.JSON(), nullable=True),
            sa.Column("source_payload", sa.JSON(), nullable=False),
            sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("jgbm", "dzdh", name="uq_sap_hana_visit_orders_jgbm_dzdh"),
        )
        existing_indexes: set[str] = set()
    else:
        existing_indexes = {index["name"] for index in inspector.get_indexes("sap_hana_visit_orders")}

    indexes = {
        "ix_sap_hana_visit_orders_jgbm": ["jgbm"],
        "ix_sap_hana_visit_orders_dzdh": ["dzdh"],
        "ix_sap_hana_visit_orders_crtdt": ["crtdt"],
        "ix_sap_hana_visit_orders_kunr": ["kunr"],
        "ix_sap_hana_visit_orders_fzuer": ["fzuer"],
    }
    for index_name, columns in indexes.items():
        if index_name not in existing_indexes:
            op.create_index(index_name, "sap_hana_visit_orders", columns, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if "sap_hana_visit_orders" not in existing_tables:
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes("sap_hana_visit_orders")}
    for index_name in (
        "ix_sap_hana_visit_orders_fzuer",
        "ix_sap_hana_visit_orders_kunr",
        "ix_sap_hana_visit_orders_crtdt",
        "ix_sap_hana_visit_orders_dzdh",
        "ix_sap_hana_visit_orders_jgbm",
    ):
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name="sap_hana_visit_orders")

    op.drop_table("sap_hana_visit_orders")
