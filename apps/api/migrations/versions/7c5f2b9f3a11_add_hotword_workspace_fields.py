"""add hotword workspace fields

Revision ID: 7c5f2b9f3a11
Revises: f14f7b7b0d21
Create Date: 2026-03-20 20:10:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "7c5f2b9f3a11"
down_revision: Union[str, Sequence[str], None] = "f14f7b7b0d21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    group_columns = {column["name"] for column in inspector.get_columns("hotword_groups")}
    if "library_scope" not in group_columns:
        op.add_column(
            "hotword_groups",
            sa.Column("library_scope", sa.String(length=20), nullable=False, server_default="public"),
        )
    if "source_label" not in group_columns:
        op.add_column(
            "hotword_groups",
            sa.Column("source_label", sa.String(length=100), nullable=False, server_default="行业"),
        )

    word_columns = {column["name"] for column in inspector.get_columns("hotwords")}
    if "weight" not in word_columns:
        op.add_column(
            "hotwords",
            sa.Column("weight", sa.Integer(), nullable=False, server_default="10"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    word_columns = {column["name"] for column in inspector.get_columns("hotwords")}
    if "weight" in word_columns:
        op.drop_column("hotwords", "weight")

    group_columns = {column["name"] for column in inspector.get_columns("hotword_groups")}
    if "source_label" in group_columns:
        op.drop_column("hotword_groups", "source_label")
    if "library_scope" in group_columns:
        op.drop_column("hotword_groups", "library_scope")
