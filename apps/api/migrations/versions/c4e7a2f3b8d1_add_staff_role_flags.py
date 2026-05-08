"""add staff role flags

Revision ID: c4e7a2f3b8d1
Revises: 8a4c1b2d9e63, b7f4d3e2c1a0
Create Date: 2026-03-24

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c4e7a2f3b8d1"
down_revision: Union[str, Sequence[str]] = ("8a4c1b2d9e63", "b7f4d3e2c1a0")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ROLE_COLUMNS = [
    "is_doctor",
    "is_nurse",
    "is_anesthetist",
    "is_cashier",
    "is_guide",
    "is_pre_advisor",
    "is_onsite_advisor",
    "is_advisor_assistant",
    "is_doctor_assistant",
    "is_vip_service",
]


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "staff" not in inspector.get_table_names():
        return

    existing = {col["name"] for col in inspector.get_columns("staff")}
    for col_name in _ROLE_COLUMNS:
        if col_name not in existing:
            op.add_column("staff", sa.Column(col_name, sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "staff" not in inspector.get_table_names():
        return

    existing = {col["name"] for col in inspector.get_columns("staff")}
    for col_name in reversed(_ROLE_COLUMNS):
        if col_name in existing:
            op.drop_column("staff", col_name)
