"""add rule_group_id to tag_categories

Revision ID: f2c7d9e4a1b3
Revises: e1a2b3c4d5f6
Create Date: 2026-03-26 16:36:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f2c7d9e4a1b3"
down_revision: Union[str, Sequence[str], None] = "e1a2b3c4d5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("tag_categories")}
    if "rule_group_id" not in columns:
        with op.batch_alter_table("tag_categories") as batch_op:
            batch_op.add_column(sa.Column("rule_group_id", sa.String(length=12), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("tag_categories")}
    if "rule_group_id" in columns:
        with op.batch_alter_table("tag_categories") as batch_op:
            batch_op.drop_column("rule_group_id")