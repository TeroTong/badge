"""add wecom tenants

Revision ID: a4f8c9d2e6b1
Revises: 6b2d9f4c8a10
Create Date: 2026-04-29 21:20:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a4f8c9d2e6b1"
down_revision: Union[str, Sequence[str], None] = "6b2d9f4c8a10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_names(inspector: sa.Inspector) -> set[str]:
    return set(inspector.get_table_names())


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _index_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _unique_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {constraint["name"] for constraint in inspector.get_unique_constraints(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = _table_names(inspector)

    if "wecom_tenants" not in tables:
        op.create_table(
            "wecom_tenants",
            sa.Column("id", sa.String(length=12), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("host", sa.String(length=255), nullable=False),
            sa.Column("corp_id", sa.String(length=100), nullable=False),
            sa.Column("agent_id", sa.String(length=100), nullable=False),
            sa.Column("agent_secret", sa.String(length=255), nullable=False),
            sa.Column("frontend_url", sa.String(length=500), nullable=False),
            sa.Column("default_hospital_code", sa.String(length=100), nullable=True),
            sa.Column("default_hospital_name", sa.String(length=255), nullable=True),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_wecom_tenants")),
            sa.UniqueConstraint("host", name="uq_wecom_tenants_host"),
        )
        op.create_index(op.f("ix_wecom_tenants_host"), "wecom_tenants", ["host"], unique=False)
        op.create_index(op.f("ix_wecom_tenants_corp_id"), "wecom_tenants", ["corp_id"], unique=False)

    inspector = sa.inspect(bind)
    tables = _table_names(inspector)
    if "staff" not in tables:
        return

    columns = _column_names(inspector, "staff")
    if "wecom_corp_id" not in columns:
        op.add_column("staff", sa.Column("wecom_corp_id", sa.String(length=100), nullable=True))

    inspector = sa.inspect(bind)
    indexes = _index_names(inspector, "staff")
    uniques = _unique_names(inspector, "staff")
    if "ix_staff_wecom_user_id" in indexes:
        op.drop_index("ix_staff_wecom_user_id", table_name="staff")
    if "uq_staff_wecom_user_id" in uniques:
        op.drop_constraint("uq_staff_wecom_user_id", "staff", type_="unique")

    inspector = sa.inspect(bind)
    indexes = _index_names(inspector, "staff")
    uniques = _unique_names(inspector, "staff")
    if "ix_staff_wecom_user_id" not in indexes:
        op.create_index("ix_staff_wecom_user_id", "staff", ["wecom_user_id"], unique=False)
    if "ix_staff_wecom_corp_id" not in indexes:
        op.create_index("ix_staff_wecom_corp_id", "staff", ["wecom_corp_id"], unique=False)
    if "uq_staff_wecom_corp_user" not in uniques:
        op.create_unique_constraint("uq_staff_wecom_corp_user", "staff", ["wecom_corp_id", "wecom_user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = _table_names(inspector)

    if "staff" in tables:
        indexes = _index_names(inspector, "staff")
        uniques = _unique_names(inspector, "staff")
        if "uq_staff_wecom_corp_user" in uniques:
            op.drop_constraint("uq_staff_wecom_corp_user", "staff", type_="unique")
        if "ix_staff_wecom_corp_id" in indexes:
            op.drop_index("ix_staff_wecom_corp_id", table_name="staff")
        if "ix_staff_wecom_user_id" in indexes:
            op.drop_index("ix_staff_wecom_user_id", table_name="staff")

        inspector = sa.inspect(bind)
        columns = _column_names(inspector, "staff")
        if "wecom_corp_id" in columns:
            op.drop_column("staff", "wecom_corp_id")
        op.create_index("ix_staff_wecom_user_id", "staff", ["wecom_user_id"], unique=True)

    inspector = sa.inspect(bind)
    tables = _table_names(inspector)
    if "wecom_tenants" in tables:
        indexes = _index_names(inspector, "wecom_tenants")
        if "ix_wecom_tenants_corp_id" in indexes:
            op.drop_index("ix_wecom_tenants_corp_id", table_name="wecom_tenants")
        if "ix_wecom_tenants_host" in indexes:
            op.drop_index("ix_wecom_tenants_host", table_name="wecom_tenants")
        op.drop_table("wecom_tenants")
