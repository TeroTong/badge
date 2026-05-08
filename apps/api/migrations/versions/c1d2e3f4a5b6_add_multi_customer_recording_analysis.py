"""add multi customer recording analysis

Revision ID: c1d2e3f4a5b6
Revises: b8a6c4d2e9f0
Create Date: 2026-05-01 11:20:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b8a6c4d2e9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "recording_customer_segments" not in table_names:
        op.create_table(
            "recording_customer_segments",
            sa.Column("id", sa.String(length=12), nullable=False),
            sa.Column("recording_id", sa.String(length=12), nullable=False),
            sa.Column("segment_index", sa.Integer(), nullable=False),
            sa.Column("label", sa.String(length=50), nullable=False),
            sa.Column("begin_ms", sa.Integer(), nullable=False),
            sa.Column("end_ms", sa.Integer(), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("utterance_indexes", sa.JSON(), nullable=False),
            sa.Column("utterance_count", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("recording_id", "segment_index", name="uq_recording_customer_segments_recording_index"),
        )
        op.create_index(
            "ix_recording_customer_segments_recording_id",
            "recording_customer_segments",
            ["recording_id"],
            unique=False,
        )

    if "recording_visit_analysis_results" not in table_names:
        op.create_table(
            "recording_visit_analysis_results",
            sa.Column("id", sa.String(length=12), nullable=False),
            sa.Column("recording_id", sa.String(length=12), nullable=False),
            sa.Column("visit_id", sa.String(length=12), nullable=False),
            sa.Column("customer_segment_id", sa.String(length=12), nullable=True),
            sa.Column("mapping_status", sa.String(length=20), nullable=False),
            sa.Column("analysis_status", sa.String(length=20), nullable=False),
            sa.Column("analysis_task_id", sa.String(length=12), nullable=True),
            sa.Column("analysis_result", sa.JSON(), nullable=True),
            sa.Column("analysis_error", sa.Text(), nullable=True),
            sa.Column("confirmed_by", sa.String(length=100), nullable=True),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sap_ready_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sap_push_log_id", sa.String(length=12), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["analysis_task_id"], ["analysis_tasks.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["customer_segment_id"], ["recording_customer_segments.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["sap_push_log_id"], ["sap_push_logs.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["visit_id"], ["visits.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("recording_id", "visit_id", name="uq_recording_visit_analysis_recording_visit"),
        )
        op.create_index(
            "ix_recording_visit_analysis_results_customer_segment_id",
            "recording_visit_analysis_results",
            ["customer_segment_id"],
            unique=False,
        )
        op.create_index(
            "ix_recording_visit_analysis_results_recording_id",
            "recording_visit_analysis_results",
            ["recording_id"],
            unique=False,
        )
        op.create_index(
            "ix_recording_visit_analysis_results_visit_id",
            "recording_visit_analysis_results",
            ["visit_id"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "recording_visit_analysis_results" in table_names:
        op.drop_index("ix_recording_visit_analysis_results_visit_id", table_name="recording_visit_analysis_results")
        op.drop_index("ix_recording_visit_analysis_results_recording_id", table_name="recording_visit_analysis_results")
        op.drop_index("ix_recording_visit_analysis_results_customer_segment_id", table_name="recording_visit_analysis_results")
        op.drop_table("recording_visit_analysis_results")

    if "recording_customer_segments" in table_names:
        op.drop_index("ix_recording_customer_segments_recording_id", table_name="recording_customer_segments")
        op.drop_table("recording_customer_segments")
