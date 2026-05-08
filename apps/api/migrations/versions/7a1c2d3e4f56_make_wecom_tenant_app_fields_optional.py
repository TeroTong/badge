"""make wecom tenant app fields optional

Revision ID: 7a1c2d3e4f56
Revises: 6d8c0f2a9b31
Create Date: 2026-04-30 17:55:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "7a1c2d3e4f56"
down_revision: Union[str, Sequence[str], None] = "6d8c0f2a9b31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


OPTIONAL_APP_COLUMNS: tuple[tuple[str, sa.String], ...] = (
    ("host", sa.String(length=255)),
    ("corp_id", sa.String(length=100)),
    ("agent_id", sa.String(length=100)),
    ("agent_secret", sa.String(length=255)),
    ("frontend_url", sa.String(length=500)),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "wecom_tenants" not in inspector.get_table_names():
        return

    columns = _column_names(inspector, "wecom_tenants")
    with op.batch_alter_table("wecom_tenants") as batch_op:
        for column_name, column_type in OPTIONAL_APP_COLUMNS:
            if column_name in columns:
                batch_op.alter_column(column_name, existing_type=column_type, nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "wecom_tenants" not in inspector.get_table_names():
        return

    tenants = sa.table(
        "wecom_tenants",
        sa.column("id", sa.String(length=12)),
        sa.column("host", sa.String(length=255)),
        sa.column("corp_id", sa.String(length=100)),
        sa.column("agent_id", sa.String(length=100)),
        sa.column("agent_secret", sa.String(length=255)),
        sa.column("frontend_url", sa.String(length=500)),
    )
    rows = bind.execute(sa.select(tenants.c.id).where(tenants.c.host.is_(None))).all()
    for (tenant_id,) in rows:
        bind.execute(
            sa.update(tenants)
            .where(tenants.c.id == tenant_id)
            .values(host=f"{tenant_id}.invalid.local")
        )
    bind.execute(sa.update(tenants).where(tenants.c.corp_id.is_(None)).values(corp_id=""))
    bind.execute(sa.update(tenants).where(tenants.c.agent_id.is_(None)).values(agent_id=""))
    bind.execute(sa.update(tenants).where(tenants.c.agent_secret.is_(None)).values(agent_secret=""))
    bind.execute(sa.update(tenants).where(tenants.c.frontend_url.is_(None)).values(frontend_url=""))

    columns = _column_names(inspector, "wecom_tenants")
    with op.batch_alter_table("wecom_tenants") as batch_op:
        for column_name, column_type in OPTIONAL_APP_COLUMNS:
            if column_name in columns:
                batch_op.alter_column(column_name, existing_type=column_type, nullable=False)
