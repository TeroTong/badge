"""add performance indexes for hot list endpoints

Revision ID: c2a8e4f10b91
Revises: b1f2c3d4e5a7
Create Date: 2026-05-08 14:55:00.000000
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "c2a8e4f10b91"
down_revision = "b1f2c3d4e5a7"
branch_labels = None
depends_on = None


# (index_name, table, columns) — 都是只读热点查询，无副作用
_INDEXES: list[tuple[str, str, list[str]]] = [
    ("ix_recordings_created_at", "recordings", ["created_at"]),
    ("ix_recordings_status", "recordings", ["status"]),
    ("ix_recordings_staff_id", "recordings", ["staff_id"]),
    ("ix_recordings_visit_id", "recordings", ["visit_id"]),
    ("ix_recordings_file_name", "recordings", ["file_name"]),
    ("ix_analysis_tasks_created_at", "analysis_tasks", ["created_at"]),
    ("ix_analysis_tasks_status", "analysis_tasks", ["status"]),
    ("ix_analysis_tasks_completed_at", "analysis_tasks", ["completed_at"]),
    ("ix_analysis_tasks_file_name", "analysis_tasks", ["file_name"]),
    ("ix_sap_push_logs_created_at", "sap_push_logs", ["created_at"]),
    ("ix_sap_push_logs_recording_id", "sap_push_logs", ["recording_id"]),
    ("ix_sap_push_logs_visit_id", "sap_push_logs", ["visit_id"]),
    ("ix_sap_push_logs_trigger_mode", "sap_push_logs", ["trigger_mode"]),
    ("ix_visits_customer_id", "visits", ["customer_id"]),
    ("ix_visits_consultant_id", "visits", ["consultant_id"]),
    ("ix_visits_doctor_id", "visits", ["doctor_id"]),
    ("ix_visits_visit_date", "visits", ["visit_date"]),
    ("ix_visits_status", "visits", ["status"]),
    ("ix_segments_recording_id", "segments", ["recording_id"]),
    ("ix_segments_visit_id", "segments", ["visit_id"]),
    ("ix_transcripts_status", "transcripts", ["status"]),
    ("ix_customers_name", "customers", ["name"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    for name, table, cols in _INDEXES:
        if dialect == "postgresql":
            col_list = ", ".join(cols)
            # IF NOT EXISTS 让重复执行幂等，避免开发库已经手动建过的冲突。
            op.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({col_list})')
        else:
            try:
                op.create_index(name, table, cols)
            except Exception:
                # 其它数据库（sqlite 测试库）若索引已存在则忽略。
                pass


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    for name, _table, _cols in reversed(_INDEXES):
        if dialect == "postgresql":
            op.execute(f'DROP INDEX IF EXISTS "{name}"')
        else:
            try:
                op.drop_index(name)
            except Exception:
                pass
