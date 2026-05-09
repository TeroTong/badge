"""add composite indexes for high-concurrency list reads

Revision ID: a7c9e1f2b4d6
Revises: d3b9f5e21c02
Create Date: 2026-05-08 23:58:00.000000
"""
from __future__ import annotations

from alembic import op


revision = "a7c9e1f2b4d6"
down_revision = "d3b9f5e21c02"
branch_labels = None
depends_on = None


_INDEXES: list[tuple[str, str, list[str]]] = [
    ("ix_recordings_staff_created_at", "recordings", ["staff_id", "created_at"]),
    ("ix_recordings_status_created_at", "recordings", ["status", "created_at"]),
    ("ix_recordings_visit_created_at", "recordings", ["visit_id", "created_at"]),
    ("ix_recording_visit_links_visit_recording", "recording_visit_links", ["visit_id", "recording_id"]),
    ("ix_recording_visit_links_recording_visit", "recording_visit_links", ["recording_id", "visit_id"]),
    ("ix_visits_customer_visit_date", "visits", ["customer_id", "visit_date"]),
    ("ix_visits_consultant_visit_date", "visits", ["consultant_id", "visit_date"]),
    ("ix_visits_doctor_visit_date", "visits", ["doctor_id", "visit_date"]),
    ("ix_visit_orders_jgbm_dzdh_dzseg", "visit_orders", ["jgbm", "dzdh", "dzseg"]),
    ("ix_staff_mgmt_manager_subordinate", "staff_management_relations", ["manager_staff_id", "subordinate_staff_id"]),
    ("ix_staff_mgmt_subordinate_manager", "staff_management_relations", ["subordinate_staff_id", "manager_staff_id"]),
    ("ix_sap_push_logs_visit_created_at", "sap_push_logs", ["visit_id", "created_at"]),
    ("ix_recording_visit_analysis_visit_status", "recording_visit_analysis_results", ["visit_id", "analysis_status"]),
    ("ix_recording_visit_analysis_recording_status", "recording_visit_analysis_results", ["recording_id", "analysis_status"]),
]


def _postgres_column_list(cols: list[str]) -> str:
    parts = []
    for col in cols:
        if col == "created_at":
            parts.append(f'"{col}" DESC')
        elif col == "visit_date":
            parts.append(f'"{col}" DESC')
        else:
            parts.append(f'"{col}"')
    return ", ".join(parts)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    for name, table, cols in _INDEXES:
        if dialect == "postgresql":
            op.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({_postgres_column_list(cols)})')
        else:
            try:
                op.create_index(name, table, cols)
            except Exception as exc:
                # Tolerate "already exists" errors only; surface anything else.
                if "already exists" not in str(exc).lower():
                    raise


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    for name, _table, _cols in reversed(_INDEXES):
        if dialect == "postgresql":
            op.execute(f'DROP INDEX IF EXISTS "{name}"')
        else:
            try:
                op.drop_index(name)
            except Exception as exc:
                if "does not exist" not in str(exc).lower():
                    raise
