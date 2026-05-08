"""add recording split metadata

Revision ID: b1f2c3d4e5a7
Revises: a8c6e4d2f9b1
Create Date: 2026-05-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b1f2c3d4e5a7"
down_revision: Union[str, Sequence[str], None] = "a8c6e4d2f9b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("recordings") as batch_op:
        batch_op.add_column(sa.Column("split_parent_recording_id", sa.String(length=12), nullable=True))
        batch_op.add_column(sa.Column("split_part_index", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("split_at_ms", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_recordings_split_parent_recording_id_recordings",
            "recordings",
            ["split_parent_recording_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_recordings_split_parent_recording_id",
            ["split_parent_recording_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("recordings") as batch_op:
        batch_op.drop_index("ix_recordings_split_parent_recording_id")
        batch_op.drop_constraint("fk_recordings_split_parent_recording_id_recordings", type_="foreignkey")
        batch_op.drop_column("split_at_ms")
        batch_op.drop_column("split_part_index")
        batch_op.drop_column("split_parent_recording_id")
