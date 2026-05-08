"""add customer demographics to visit_orders

Revision ID: d5a3b7c9e2f1
Revises: c4e7a2f3b8d1
Create Date: 2026-03-25

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5a3b7c9e2f1"
down_revision: Union[str, Sequence[str]] = "c4e7a2f3b8d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_COLUMNS = [
    ("customer_gender", sa.String(10)),
    ("customer_birthday", sa.String(10)),
]


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "visit_orders" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("visit_orders")}
    for col_name, col_type in _NEW_COLUMNS:
        if col_name not in existing:
            op.add_column("visit_orders", sa.Column(col_name, col_type, nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "visit_orders" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("visit_orders")}
    for col_name, _ in reversed(_NEW_COLUMNS):
        if col_name in existing:
            op.drop_column("visit_orders", col_name)
