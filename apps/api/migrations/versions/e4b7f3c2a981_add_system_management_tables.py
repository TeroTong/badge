"""add system management tables

Revision ID: e4b7f3c2a981
Revises: 9d2e3c4a1b55
Create Date: 2026-03-21 03:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "e4b7f3c2a981"
down_revision = "9d2e3c4a1b55"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    existing_tables = set(inspector.get_table_names())

    if "teams" not in existing_tables:
        op.create_table(
            "teams",
            sa.Column("id", sa.String(length=12), primary_key=True, nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False, unique=True),
            sa.Column("note", sa.Text(), nullable=False, server_default=""),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "position_profiles" not in existing_tables:
        op.create_table(
            "position_profiles",
            sa.Column("id", sa.String(length=12), primary_key=True, nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False, unique=True),
            sa.Column("position_type", sa.String(length=50), nullable=False, server_default="staff"),
            sa.Column("mapped_role", sa.String(length=50), nullable=False, server_default="consultant"),
            sa.Column("is_super_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("available_services", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("note", sa.Text(), nullable=False, server_default=""),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "audit_logs" not in existing_tables:
        op.create_table(
            "audit_logs",
            sa.Column("id", sa.String(length=12), primary_key=True, nullable=False),
            sa.Column("operator_name", sa.String(length=100), nullable=False),
            sa.Column("ip_address", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("module_name", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("action_name", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "seats" not in existing_tables:
        op.create_table(
            "seats",
            sa.Column("id", sa.String(length=12), primary_key=True, nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("code", sa.String(length=100), nullable=True),
            sa.Column("team_id", sa.String(length=12), nullable=True),
            sa.Column("staff_id", sa.String(length=12), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="idle"),
            sa.Column("note", sa.Text(), nullable=False, server_default=""),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    staff_columns = {column["name"] for column in inspector.get_columns("staff")}
    if "phone" not in staff_columns:
        op.add_column("staff", sa.Column("phone", sa.String(length=20), nullable=True))
    if "external_account" not in staff_columns:
        op.add_column("staff", sa.Column("external_account", sa.String(length=100), nullable=True))
    if "gender" not in staff_columns:
        op.add_column("staff", sa.Column("gender", sa.String(length=10), nullable=True))
    if "team_id" not in staff_columns:
        op.add_column("staff", sa.Column("team_id", sa.String(length=12), nullable=True))
    if "position_id" not in staff_columns:
        op.add_column("staff", sa.Column("position_id", sa.String(length=12), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    staff_columns = {column["name"] for column in inspector.get_columns("staff")}

    if "position_id" in staff_columns:
        op.drop_column("staff", "position_id")
    if "team_id" in staff_columns:
        op.drop_column("staff", "team_id")
    if "gender" in staff_columns:
        op.drop_column("staff", "gender")
    if "external_account" in staff_columns:
        op.drop_column("staff", "external_account")
    if "phone" in staff_columns:
        op.drop_column("staff", "phone")

    existing_tables = set(inspector.get_table_names())
    if "seats" in existing_tables:
        op.drop_table("seats")
    if "audit_logs" in existing_tables:
        op.drop_table("audit_logs")
    if "position_profiles" in existing_tables:
        op.drop_table("position_profiles")
    if "teams" in existing_tables:
        op.drop_table("teams")
