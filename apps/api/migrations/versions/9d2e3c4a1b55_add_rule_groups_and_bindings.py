"""add rule groups and bindings

Revision ID: 9d2e3c4a1b55
Revises: 7c5f2b9f3a11
Create Date: 2026-03-20 20:55:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "9d2e3c4a1b55"
down_revision: Union[str, Sequence[str], None] = "7c5f2b9f3a11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "rule_groups" not in inspector.get_table_names():
        op.create_table(
            "rule_groups",
            sa.Column("id", sa.String(length=12), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("note", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(length=100), nullable=False, server_default="admin"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_rule_groups")),
            sa.UniqueConstraint("name", name=op.f("uq_rule_groups_name")),
        )

    template_columns = {column["name"] for column in inspector.get_columns("summary_templates")}
    if "rule_group_id" not in template_columns:
        op.add_column("summary_templates", sa.Column("rule_group_id", sa.String(length=12), nullable=True))

    dimension_columns = {column["name"] for column in inspector.get_columns("quality_dimensions")}
    if "rule_group_id" not in dimension_columns:
        op.add_column("quality_dimensions", sa.Column("rule_group_id", sa.String(length=12), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    dimension_columns = {column["name"] for column in inspector.get_columns("quality_dimensions")}
    if "rule_group_id" in dimension_columns:
        op.drop_column("quality_dimensions", "rule_group_id")

    template_columns = {column["name"] for column in inspector.get_columns("summary_templates")}
    if "rule_group_id" in template_columns:
        op.drop_column("summary_templates", "rule_group_id")

    if "rule_groups" in inspector.get_table_names():
        op.drop_table("rule_groups")
