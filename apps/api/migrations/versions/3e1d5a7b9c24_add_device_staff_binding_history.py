"""add device staff binding history

Revision ID: 3e1d5a7b9c24
Revises: 9c3e7a1d5b42, a1c4d7e8f901, c7a5e9d1b204
Create Date: 2026-04-14 00:00:00.000000
"""

from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "3e1d5a7b9c24"
down_revision = ("9c3e7a1d5b42", "a1c4d7e8f901", "c7a5e9d1b204")
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "device_staff_bindings" not in existing_tables:
        op.create_table(
            "device_staff_bindings",
            sa.Column("id", sa.String(length=12), nullable=False),
            sa.Column("device_id", sa.String(length=12), nullable=False),
            sa.Column("staff_id", sa.String(length=12), nullable=False),
            sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
            sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["staff_id"], ["staff.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "device_id",
                "staff_id",
                "effective_from",
                name="uq_device_staff_bindings_device_staff_from",
            ),
        )

    existing_indexes = {index["name"] for index in inspector.get_indexes("device_staff_bindings")}
    if "ix_device_staff_bindings_device_id" not in existing_indexes:
        op.create_index("ix_device_staff_bindings_device_id", "device_staff_bindings", ["device_id"], unique=False)
    if "ix_device_staff_bindings_staff_id" not in existing_indexes:
        op.create_index("ix_device_staff_bindings_staff_id", "device_staff_bindings", ["staff_id"], unique=False)
    if "ix_device_staff_bindings_effective_from" not in existing_indexes:
        op.create_index("ix_device_staff_bindings_effective_from", "device_staff_bindings", ["effective_from"], unique=False)
    if "ix_device_staff_bindings_effective_to" not in existing_indexes:
        op.create_index("ix_device_staff_bindings_effective_to", "device_staff_bindings", ["effective_to"], unique=False)

    current_rows = bind.execute(
        sa.text(
            """
            SELECT d.id AS device_id,
                   d.staff_id AS staff_id,
                   COALESCE(d.updated_at, d.created_at, CURRENT_TIMESTAMP) AS effective_from
            FROM devices AS d
            LEFT JOIN device_staff_bindings AS b
              ON b.device_id = d.id AND b.effective_to IS NULL
            WHERE d.staff_id IS NOT NULL
              AND b.id IS NULL
            """
        )
    ).mappings().all()

    for row in current_rows:
        effective_from = row["effective_from"]
        bind.execute(
            sa.text(
                """
                INSERT INTO device_staff_bindings
                    (id, device_id, staff_id, effective_from, effective_to, created_at, updated_at)
                VALUES
                    (:id, :device_id, :staff_id, :effective_from, NULL, :created_at, :updated_at)
                """
            ),
            {
                "id": uuid.uuid4().hex[:12],
                "device_id": row["device_id"],
                "staff_id": row["staff_id"],
                "effective_from": effective_from,
                "created_at": effective_from,
                "updated_at": effective_from,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if "device_staff_bindings" not in existing_tables:
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes("device_staff_bindings")}
    for index_name in (
        "ix_device_staff_bindings_effective_to",
        "ix_device_staff_bindings_effective_from",
        "ix_device_staff_bindings_staff_id",
        "ix_device_staff_bindings_device_id",
    ):
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name="device_staff_bindings")

    op.drop_table("device_staff_bindings")
