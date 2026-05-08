"""normalize legacy team scope and remove team entities

Revision ID: 9a1f2b3c4d5e
Revises: f8c1e2d3b4a5
Create Date: 2026-04-04 20:15:00.000000
"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "9a1f2b3c4d5e"
down_revision: Union[str, Sequence[str], None] = "f8c1e2d3b4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _index_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _normalize_preference_config(raw: object) -> tuple[object, bool]:
    if not isinstance(raw, dict):
        return raw, False

    changed = False
    config = dict(raw)

    if config.get("pending_customer_scope") == "team_shared":
        config["pending_customer_scope"] = "hospital_shared"
        changed = True

    role_bridge_settings = config.get("role_bridge_settings")
    if isinstance(role_bridge_settings, list):
        normalized_items: list[object] = []
        for item in role_bridge_settings:
            if not isinstance(item, dict):
                normalized_items.append(item)
                continue
            next_item = dict(item)
            if next_item.get("display_scope") == "team":
                next_item["display_scope"] = "all"
                changed = True
            normalized_items.append(next_item)
        config["role_bridge_settings"] = normalized_items

    return config, changed


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "users" in tables:
        bind.execute(sa.text("UPDATE users SET role = 'hospital_admin' WHERE role = 'team_admin'"))

    if "staff" in tables:
        bind.execute(sa.text("UPDATE staff SET permission_role = 'hospital_admin' WHERE permission_role = 'team_admin'"))

    if "position_profiles" in tables:
        rows = bind.execute(
            sa.text("SELECT id, name, mapped_role FROM position_profiles ORDER BY created_at ASC, id ASC")
        ).mappings().all()

        hospital_admin_id = None
        team_position_ids: list[str] = []
        for row in rows:
            if row["mapped_role"] == "hospital_admin" or row["name"] == "医院管理员":
                hospital_admin_id = row["id"]
            if row["mapped_role"] == "team_admin" or row["name"] == "团队管理员":
                team_position_ids.append(row["id"])

        if hospital_admin_id is None and team_position_ids:
            hospital_admin_id = team_position_ids[0]
            bind.execute(
                sa.text(
                    """
                    UPDATE position_profiles
                    SET name = '医院管理员',
                        mapped_role = 'hospital_admin',
                        note = '管理医院范围内的员工与业务数据'
                    WHERE id = :position_id
                    """
                ),
                {"position_id": hospital_admin_id},
            )
            team_position_ids = team_position_ids[1:]

        if hospital_admin_id:
            for team_position_id in team_position_ids:
                bind.execute(
                    sa.text(
                        "UPDATE staff SET position_id = :hospital_position_id WHERE position_id = :team_position_id"
                    ),
                    {
                        "hospital_position_id": hospital_admin_id,
                        "team_position_id": team_position_id,
                    },
                )

        bind.execute(
            sa.text(
                """
                UPDATE position_profiles
                SET note = ''
                WHERE mapped_role = 'org_admin'
                """
            )
        )
        bind.execute(
            sa.text(
                """
                UPDATE position_profiles
                SET note = '管理医院范围内的员工与业务数据'
                WHERE mapped_role = 'hospital_admin'
                """
            )
        )

        for team_position_id in team_position_ids:
            bind.execute(sa.text("DELETE FROM position_profiles WHERE id = :position_id"), {"position_id": team_position_id})

    if "preference_profiles" in tables:
        rows = bind.execute(sa.text("SELECT id, config FROM preference_profiles")).mappings().all()
        for row in rows:
            config = row["config"]
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except json.JSONDecodeError:
                    continue
            normalized, changed = _normalize_preference_config(config)
            if changed:
                bind.execute(
                    sa.text(
                        "UPDATE preference_profiles SET config = :config WHERE id = :profile_id"
                        if bind.dialect.name == "sqlite"
                        else "UPDATE preference_profiles SET config = CAST(:config AS JSON) WHERE id = :profile_id"
                    ),
                    {"profile_id": row["id"], "config": json.dumps(normalized, ensure_ascii=False)},
                )

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "users" in tables:
        user_indexes = _index_names(inspector, "users")
        user_columns = _column_names(inspector, "users")
        if "ix_users_team_id" in user_indexes:
            op.drop_index("ix_users_team_id", table_name="users")
        if "team_id" in user_columns:
            op.drop_column("users", "team_id")

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "staff" in tables and "team_id" in _column_names(inspector, "staff"):
        op.drop_column("staff", "team_id")

    if "devices" in tables and "team_id" in _column_names(inspector, "devices"):
        op.drop_column("devices", "team_id")

    if "seats" in tables and "team_id" in _column_names(inspector, "seats"):
        op.drop_column("seats", "team_id")

    inspector = sa.inspect(bind)
    if "teams" in inspector.get_table_names():
        op.drop_table("teams")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "teams" not in tables:
        op.create_table(
            "teams",
            sa.Column("id", sa.String(length=12), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("organization_code", sa.String(length=20), nullable=True),
            sa.Column("organization_name", sa.String(length=100), nullable=True),
            sa.Column("hospital_code", sa.String(length=20), nullable=True),
            sa.Column("hospital_name", sa.String(length=100), nullable=True),
            sa.Column("note", sa.Text(), nullable=False, server_default=""),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )
        op.create_index("ix_teams_organization_code", "teams", ["organization_code"], unique=False)
        op.create_index("ix_teams_hospital_code", "teams", ["hospital_code"], unique=False)

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "users" in tables and "team_id" not in _column_names(inspector, "users"):
        op.add_column("users", sa.Column("team_id", sa.String(length=12), nullable=True))
        op.create_index("ix_users_team_id", "users", ["team_id"], unique=False)

    if "staff" in tables and "team_id" not in _column_names(inspector, "staff"):
        op.add_column("staff", sa.Column("team_id", sa.String(length=12), nullable=True))

    if "devices" in tables and "team_id" not in _column_names(inspector, "devices"):
        op.add_column("devices", sa.Column("team_id", sa.String(length=12), nullable=True))

    if "seats" in tables and "team_id" not in _column_names(inspector, "seats"):
        op.add_column("seats", sa.Column("team_id", sa.String(length=12), nullable=True))
