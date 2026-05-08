"""add recording visit links

Revision ID: e1a2b3c4d5f6
Revises: d5a3b7c9e2f1, 9d2e3c4a1b55
Create Date: 2026-03-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e1a2b3c4d5f6"
down_revision: Union[str, Sequence[str], None] = ("d5a3b7c9e2f1", "9d2e3c4a1b55")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recording_visit_links",
        sa.Column("id", sa.String(length=12), nullable=False),
        sa.Column("recording_id", sa.String(length=12), nullable=False),
        sa.Column("visit_id", sa.String(length=12), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(length=30), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["visit_id"], ["visits.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recording_id", "visit_id", name="uq_recording_visit_links_recording_visit"),
    )
    op.create_index(op.f("ix_recording_visit_links_recording_id"), "recording_visit_links", ["recording_id"], unique=False)
    op.create_index(op.f("ix_recording_visit_links_visit_id"), "recording_visit_links", ["visit_id"], unique=False)

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, visit_id FROM recordings WHERE visit_id IS NOT NULL")).fetchall()
    for recording_id, visit_id in rows:
        conn.execute(
            sa.text(
                """
                INSERT INTO recording_visit_links (id, recording_id, visit_id, is_primary, source, created_at, updated_at)
                VALUES (:id, :recording_id, :visit_id, :is_primary, :source, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            ),
            {
                "id": f"rvl{recording_id}"[:12],
                "recording_id": recording_id,
                "visit_id": visit_id,
                "is_primary": True,
                "source": "backfill",
            },
        )

    with op.batch_alter_table("recording_visit_links") as batch_op:
        batch_op.alter_column("is_primary", server_default=None)


def downgrade() -> None:
    op.drop_index(op.f("ix_recording_visit_links_visit_id"), table_name="recording_visit_links")
    op.drop_index(op.f("ix_recording_visit_links_recording_id"), table_name="recording_visit_links")
    op.drop_table("recording_visit_links")