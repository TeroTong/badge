"""add legacy permission hierarchy and scope fields

Revision ID: f8c1e2d3b4a5
Revises: 6f2d4c1a9b87, e5a7b2c3d4f1
Create Date: 2026-04-04 18:30:00.000000
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op


revision: str = "f8c1e2d3b4a5"
down_revision: Union[str, Sequence[str], None] = ("6f2d4c1a9b87", "e5a7b2c3d4f1")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _index_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _is_sqlite(bind: sa.Connection) -> bool:
    return bind.dialect.name == "sqlite"


def _sync_users_from_staff(bind: sa.Connection) -> None:
    if _is_sqlite(bind):
        bind.execute(
            sa.text(
                """
                UPDATE users
                SET role = COALESCE(NULLIF((
                        SELECT s.permission_role
                        FROM staff s
                        WHERE users.staff_id = s.id
                    ), ''), role),
                    organization_code = (
                        SELECT s.organization_code
                        FROM staff s
                        WHERE users.staff_id = s.id
                    ),
                    organization_name = (
                        SELECT s.organization_name
                        FROM staff s
                        WHERE users.staff_id = s.id
                    ),
                    hospital_code = (
                        SELECT s.hospital_code
                        FROM staff s
                        WHERE users.staff_id = s.id
                    ),
                    hospital_name = (
                        SELECT s.hospital_short_name
                        FROM staff s
                        WHERE users.staff_id = s.id
                    ),
                    team_id = (
                        SELECT s.team_id
                        FROM staff s
                        WHERE users.staff_id = s.id
                    )
                WHERE EXISTS (
                    SELECT 1
                    FROM staff s
                    WHERE users.staff_id = s.id
                )
                """
            )
        )
        return

    bind.execute(
        sa.text(
            """
            UPDATE users u
            SET role = COALESCE(NULLIF(s.permission_role, ''), u.role),
                organization_code = s.organization_code,
                organization_name = s.organization_name,
                hospital_code = s.hospital_code,
                hospital_name = s.hospital_short_name,
                team_id = s.team_id
            FROM staff s
            WHERE u.staff_id = s.id
            """
        )
    )


def _ensure_position_profiles(bind: sa.Connection) -> None:
    position_profiles = sa.table(
        "position_profiles",
        sa.column("id", sa.String(length=12)),
        sa.column("name", sa.String(length=100)),
        sa.column("position_type", sa.String(length=50)),
        sa.column("mapped_role", sa.String(length=50)),
        sa.column("is_super_admin", sa.Boolean()),
        sa.column("available_services", sa.JSON()),
        sa.column("note", sa.Text()),
        sa.column("is_active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )

    rows = [
        ("超级管理员", "management", "super_admin", True, "平台最高权限，唯一账号使用"),
        ("系统管理员", "management", "system_admin", False, "全局系统管理与业务权限配置"),
        ("机构管理员", "management", "org_admin", False, ""),
        ("医院管理员", "management", "hospital_admin", False, "管理医院范围内的员工与业务数据"),
        ("团队管理员", "management", "team_admin", False, ""),
    ]
    existing_names = {
        row[0]
        for row in bind.execute(sa.text("SELECT name FROM position_profiles"))
    }
    timestamp = datetime.now(timezone.utc)

    for name, position_type, mapped_role, is_super_admin, note in rows:
        if name in existing_names:
            continue
        bind.execute(
            position_profiles.insert().values(
                id=uuid4().hex[:12],
                name=name,
                position_type=position_type,
                mapped_role=mapped_role,
                is_super_admin=is_super_admin,
                available_services=[],
                note=note,
                is_active=True,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "users" in tables:
        user_columns = _column_names(inspector, "users")
        if "organization_code" not in user_columns:
            op.add_column("users", sa.Column("organization_code", sa.String(length=20), nullable=True))
        if "organization_name" not in user_columns:
            op.add_column("users", sa.Column("organization_name", sa.String(length=100), nullable=True))
        if "hospital_code" not in user_columns:
            op.add_column("users", sa.Column("hospital_code", sa.String(length=20), nullable=True))
        if "hospital_name" not in user_columns:
            op.add_column("users", sa.Column("hospital_name", sa.String(length=100), nullable=True))
        if "team_id" not in user_columns:
            op.add_column("users", sa.Column("team_id", sa.String(length=12), nullable=True))
        if not _is_sqlite(bind):
            op.alter_column(
                "users",
                "role",
                existing_type=sa.String(length=20),
                type_=sa.String(length=30),
                existing_nullable=False,
            )

    if "teams" in tables:
        team_columns = _column_names(inspector, "teams")
        if "organization_code" not in team_columns:
            op.add_column("teams", sa.Column("organization_code", sa.String(length=20), nullable=True))
        if "organization_name" not in team_columns:
            op.add_column("teams", sa.Column("organization_name", sa.String(length=100), nullable=True))
        if "hospital_code" not in team_columns:
            op.add_column("teams", sa.Column("hospital_code", sa.String(length=20), nullable=True))
        if "hospital_name" not in team_columns:
            op.add_column("teams", sa.Column("hospital_name", sa.String(length=100), nullable=True))

    if "staff" in tables:
        staff_columns = _column_names(inspector, "staff")
        if "organization_code" not in staff_columns:
            op.add_column("staff", sa.Column("organization_code", sa.String(length=20), nullable=True))
        if "organization_name" not in staff_columns:
            op.add_column("staff", sa.Column("organization_name", sa.String(length=100), nullable=True))
        if "permission_role" not in staff_columns:
            op.add_column("staff", sa.Column("permission_role", sa.String(length=30), nullable=True))

    if "users" in tables:
        user_indexes = _index_names(inspector, "users")
        if "ix_users_organization_code" not in user_indexes:
            op.create_index("ix_users_organization_code", "users", ["organization_code"], unique=False)
        if "ix_users_hospital_code" not in user_indexes:
            op.create_index("ix_users_hospital_code", "users", ["hospital_code"], unique=False)
        if "ix_users_team_id" not in user_indexes:
            op.create_index("ix_users_team_id", "users", ["team_id"], unique=False)

    if "teams" in tables:
        team_indexes = _index_names(inspector, "teams")
        if "ix_teams_organization_code" not in team_indexes:
            op.create_index("ix_teams_organization_code", "teams", ["organization_code"], unique=False)
        if "ix_teams_hospital_code" not in team_indexes:
            op.create_index("ix_teams_hospital_code", "teams", ["hospital_code"], unique=False)

    if "staff" in tables:
        staff_indexes = _index_names(inspector, "staff")
        if "ix_staff_organization_code" not in staff_indexes:
            op.create_index("ix_staff_organization_code", "staff", ["organization_code"], unique=False)
        if "ix_staff_permission_role" not in staff_indexes:
            op.create_index("ix_staff_permission_role", "staff", ["permission_role"], unique=False)

    if "position_profiles" in tables:
        bind.execute(sa.text("""
            UPDATE position_profiles
            SET mapped_role = CASE mapped_role
                WHEN 'admin' THEN 'system_admin'
                WHEN 'manager' THEN 'team_admin'
                WHEN 'consultant' THEN 'staff'
                ELSE mapped_role
            END
        """))

    if "staff" in tables:
        bind.execute(sa.text("""
            UPDATE staff
            SET permission_role = CASE role
                WHEN 'admin' THEN 'system_admin'
                WHEN 'manager' THEN 'team_admin'
                ELSE COALESCE(permission_role, 'staff')
            END
            WHERE permission_role IS NULL OR permission_role = ''
        """))

        bind.execute(sa.text("""
            UPDATE staff
            SET role = CASE
                WHEN role IN ('admin', 'manager') THEN 'consultant'
                ELSE role
            END
        """))

        bind.execute(sa.text("""
            UPDATE staff
            SET organization_code = (
                    SELECT vo.jgbm
                    FROM visit_orders vo
                    WHERE vo.yybm = staff.hospital_code AND vo.jgbm IS NOT NULL
                    ORDER BY vo.synced_at DESC
                    LIMIT 1
                )
            WHERE organization_code IS NULL AND hospital_code IS NOT NULL
        """))

    if "users" in tables:
        bind.execute(sa.text("""
            UPDATE users
            SET role = CASE role
                WHEN 'admin' THEN 'system_admin'
                WHEN 'manager' THEN 'team_admin'
                WHEN 'viewer' THEN 'staff'
                ELSE role
            END
        """))

    if "teams" in tables and "staff" in tables:
        bind.execute(sa.text("""
            UPDATE teams
            SET hospital_code = (
                    SELECT s.hospital_code
                    FROM staff s
                    WHERE s.team_id = teams.id AND s.hospital_code IS NOT NULL
                    ORDER BY s.updated_at DESC
                    LIMIT 1
                ),
                hospital_name = (
                    SELECT s.hospital_short_name
                    FROM staff s
                    WHERE s.team_id = teams.id AND s.hospital_short_name IS NOT NULL
                    ORDER BY s.updated_at DESC
                    LIMIT 1
                ),
                organization_code = (
                    SELECT s.organization_code
                    FROM staff s
                    WHERE s.team_id = teams.id AND s.organization_code IS NOT NULL
                    ORDER BY s.updated_at DESC
                    LIMIT 1
                ),
                organization_name = (
                    SELECT s.organization_name
                    FROM staff s
                    WHERE s.team_id = teams.id AND s.organization_name IS NOT NULL
                    ORDER BY s.updated_at DESC
                    LIMIT 1
                )
            WHERE EXISTS (SELECT 1 FROM staff s WHERE s.team_id = teams.id)
        """))

    if "users" in tables and "staff" in tables:
        _sync_users_from_staff(bind)

    if "position_profiles" in tables:
        _ensure_position_profiles(bind)

    if "users" in tables:
        has_super_admin = bind.execute(sa.text("SELECT COUNT(1) FROM users WHERE role = 'super_admin'")).scalar() or 0
        if not has_super_admin:
            admin_user_id = bind.execute(sa.text("SELECT id FROM users WHERE username = 'admin' LIMIT 1")).scalar()
            if admin_user_id:
                bind.execute(sa.text("UPDATE users SET role = 'super_admin' WHERE id = :user_id"), {"user_id": admin_user_id})


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "users" in tables:
        bind.execute(sa.text("""
            UPDATE users
            SET role = CASE role
                WHEN 'super_admin' THEN 'admin'
                WHEN 'system_admin' THEN 'admin'
                WHEN 'org_admin' THEN 'manager'
                WHEN 'hospital_admin' THEN 'manager'
                WHEN 'team_admin' THEN 'manager'
                WHEN 'staff' THEN 'viewer'
                ELSE role
            END
        """))

    if "staff" in tables:
        bind.execute(sa.text("""
            UPDATE staff
            SET role = CASE permission_role
                WHEN 'super_admin' THEN 'admin'
                WHEN 'system_admin' THEN 'admin'
                WHEN 'org_admin' THEN 'manager'
                WHEN 'hospital_admin' THEN 'manager'
                WHEN 'team_admin' THEN 'manager'
                ELSE role
            END
        """))

    if "position_profiles" in tables:
        bind.execute(sa.text("""
            UPDATE position_profiles
            SET mapped_role = CASE mapped_role
                WHEN 'super_admin' THEN 'admin'
                WHEN 'system_admin' THEN 'admin'
                WHEN 'org_admin' THEN 'manager'
                WHEN 'hospital_admin' THEN 'manager'
                WHEN 'team_admin' THEN 'manager'
                WHEN 'staff' THEN 'consultant'
                ELSE mapped_role
            END
        """))

    if "staff" in tables:
        staff_columns = _column_names(inspector, "staff")
        staff_indexes = _index_names(inspector, "staff")
        if "ix_staff_permission_role" in staff_indexes:
            op.drop_index("ix_staff_permission_role", table_name="staff")
        if "ix_staff_organization_code" in staff_indexes:
            op.drop_index("ix_staff_organization_code", table_name="staff")
        if "permission_role" in staff_columns:
            op.drop_column("staff", "permission_role")
        if "organization_name" in staff_columns:
            op.drop_column("staff", "organization_name")
        if "organization_code" in staff_columns:
            op.drop_column("staff", "organization_code")

    if "teams" in tables:
        team_columns = _column_names(inspector, "teams")
        team_indexes = _index_names(inspector, "teams")
        if "ix_teams_hospital_code" in team_indexes:
            op.drop_index("ix_teams_hospital_code", table_name="teams")
        if "ix_teams_organization_code" in team_indexes:
            op.drop_index("ix_teams_organization_code", table_name="teams")
        if "hospital_name" in team_columns:
            op.drop_column("teams", "hospital_name")
        if "hospital_code" in team_columns:
            op.drop_column("teams", "hospital_code")
        if "organization_name" in team_columns:
            op.drop_column("teams", "organization_name")
        if "organization_code" in team_columns:
            op.drop_column("teams", "organization_code")

    if "users" in tables:
        user_columns = _column_names(inspector, "users")
        user_indexes = _index_names(inspector, "users")
        if "ix_users_team_id" in user_indexes:
            op.drop_index("ix_users_team_id", table_name="users")
        if "ix_users_hospital_code" in user_indexes:
            op.drop_index("ix_users_hospital_code", table_name="users")
        if "ix_users_organization_code" in user_indexes:
            op.drop_index("ix_users_organization_code", table_name="users")
        if "team_id" in user_columns:
            op.drop_column("users", "team_id")
        if "hospital_name" in user_columns:
            op.drop_column("users", "hospital_name")
        if "hospital_code" in user_columns:
            op.drop_column("users", "hospital_code")
        if "organization_name" in user_columns:
            op.drop_column("users", "organization_name")
        if "organization_code" in user_columns:
            op.drop_column("users", "organization_code")
        if not _is_sqlite(bind):
            op.alter_column(
                "users",
                "role",
                existing_type=sa.String(length=30),
                type_=sa.String(length=20),
                existing_nullable=False,
            )
