"""add index on audit_logs.created_at

Revision ID: d3b9f5e21c02
Revises: c2a8e4f10b91
Create Date: 2026-05-08 15:10:00.000000
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "d3b9f5e21c02"
down_revision = "c2a8e4f10b91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute('CREATE INDEX IF NOT EXISTS "ix_audit_logs_created_at" ON "audit_logs" (created_at)')
    else:
        try:
            op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
        except Exception:
            pass


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute('DROP INDEX IF EXISTS "ix_audit_logs_created_at"')
    else:
        try:
            op.drop_index("ix_audit_logs_created_at")
        except Exception:
            pass
