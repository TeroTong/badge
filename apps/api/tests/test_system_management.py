import asyncio
import os

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.positions import _ensure_position_not_in_use
from smart_badge_api.api.routes.staff import bulk_import_staff_rows
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import PositionProfile, Staff
from smart_badge_api.db.system_defaults import (
    DEFAULT_AUDIT_LOGS,
    DEFAULT_POSITIONS,
    DEFAULT_STAFF,
    LEGACY_SUPER_ADMIN_POSITION_NAME,
    SUPER_ADMIN_POSITION_NAME,
    ensure_system_positions,
    ensure_system_sample_staff,
)
from smart_badge_api.schemas.staff import StaffImportRow


def test_system_management_defaults_cover_primary_entities() -> None:
    position_names = {item["name"] for item in DEFAULT_POSITIONS}
    mapped_roles = {item["mapped_role"] for item in DEFAULT_POSITIONS}

    assert {SUPER_ADMIN_POSITION_NAME, "系统管理员", "机构管理员", "普通员工"} <= position_names
    assert {"super_admin", "system_admin", "hospital_admin", "staff"} <= mapped_roles


def test_default_staff_reference_existing_positions() -> None:
    position_names = {item["position_name"] for item in DEFAULT_STAFF}
    configured_position_names = {item["name"] for item in DEFAULT_POSITIONS}

    assert position_names <= configured_position_names


def test_default_audit_logs_cover_login_and_user_events() -> None:
    module_names = {item["module_name"] for item in DEFAULT_AUDIT_LOGS}
    action_names = {item["action_name"] for item in DEFAULT_AUDIT_LOGS}

    assert "登录系统" in module_names
    assert "新增用户" in module_names
    assert "账号密码登录" in action_names


def test_system_defaults_do_not_overwrite_manual_changes() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        previous_value = os.environ.get("SMART_BADGE_ENABLE_SAMPLE_STAFF")
        os.environ["SMART_BADGE_ENABLE_SAMPLE_STAFF"] = "1"

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                await ensure_system_positions(db)
                await ensure_system_sample_staff(db)

                position = (
                    await db.execute(
                        select(PositionProfile).where(PositionProfile.name == DEFAULT_POSITIONS[1]["name"])
                    )
                ).scalar_one()
                staff = (
                    await db.execute(select(Staff).where(Staff.name == DEFAULT_STAFF[2]["name"]))
                ).scalar_one()

                position.note = "手工修改岗位备注"
                staff.phone = "19999999999"
                await db.commit()

                await ensure_system_positions(db)
                await ensure_system_sample_staff(db)

                refreshed_position = await db.get(PositionProfile, position.id)
                refreshed_staff = await db.get(Staff, staff.id)

                assert refreshed_position.note == "手工修改岗位备注"
                assert refreshed_staff.phone == "19999999999"
        finally:
            if previous_value is None:
                os.environ.pop("SMART_BADGE_ENABLE_SAMPLE_STAFF", None)
            else:
                os.environ["SMART_BADGE_ENABLE_SAMPLE_STAFF"] = previous_value
            await engine.dispose()

    asyncio.run(scenario())


def test_system_positions_rename_legacy_super_admin_name_in_place() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                legacy_position = PositionProfile(
                    name=LEGACY_SUPER_ADMIN_POSITION_NAME,
                    mapped_role="staff",
                    is_super_admin=False,
                    position_type="staff",
                    note="旧岗位备注",
                )
                db.add(legacy_position)
                await db.commit()

                await ensure_system_positions(db)

                positions = (await db.execute(select(PositionProfile))).scalars().all()
                assert len([item for item in positions if item.name == SUPER_ADMIN_POSITION_NAME]) == 1
                assert all(item.name != LEGACY_SUPER_ADMIN_POSITION_NAME for item in positions)

                refreshed = await db.get(PositionProfile, legacy_position.id)
                assert refreshed is not None
                assert refreshed.name == SUPER_ADMIN_POSITION_NAME
                assert refreshed.mapped_role == "super_admin"
                assert refreshed.is_super_admin is True
                assert refreshed.position_type == "management"
                assert refreshed.note == "平台最高权限，唯一账号使用"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_system_positions_merge_duplicate_super_admin_positions() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                canonical_position = PositionProfile(name=SUPER_ADMIN_POSITION_NAME)
                legacy_position = PositionProfile(name=LEGACY_SUPER_ADMIN_POSITION_NAME)
                db.add(canonical_position)
                db.add(legacy_position)
                await db.flush()
                db.add(
                    Staff(
                        name="超级管理测试",
                        hospital_code="H001",
                        hospital_short_name="锦城店",
                        position_id=legacy_position.id,
                        role="consultant",
                    )
                )
                await db.commit()

                await ensure_system_positions(db)

                positions = (await db.execute(select(PositionProfile))).scalars().all()
                assert len([item for item in positions if item.name == SUPER_ADMIN_POSITION_NAME]) == 1
                assert all(item.name != LEGACY_SUPER_ADMIN_POSITION_NAME for item in positions)

                staff = (await db.execute(select(Staff).where(Staff.name == "超级管理测试"))).scalar_one()
                assert staff.position_id == canonical_position.id
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_delete_guard_rejects_referenced_position() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                position = PositionProfile(name="普通员工")
                db.add(position)
                await db.flush()
                db.add(
                    Staff(
                        name="杜娟",
                        hospital_code="H001",
                        hospital_short_name="锦城店",
                        position_id=position.id,
                        role="consultant",
                    )
                )
                await db.commit()

                try:
                    await _ensure_position_not_in_use(db, position.id)
                except HTTPException as exc:
                    assert exc.status_code == 400
                    assert "人员引用" in exc.detail
                else:
                    raise AssertionError("Position delete guard should reject referenced positions")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_bulk_import_staff_is_atomic_when_any_row_is_invalid() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                await ensure_system_positions(db)

                try:
                    await bulk_import_staff_rows(
                        db,
                        [
                            StaffImportRow(
                                name="张三",
                                phone="13800000000",
                                hospital_code="H001",
                                hospital_short_name="锦城店",
                                position_name=DEFAULT_POSITIONS[-1]["name"],
                            ),
                            StaffImportRow(
                                name="李四",
                                phone="13800000001",
                                hospital_code="H001",
                                hospital_short_name="锦城店",
                                position_name="不存在岗位",
                            ),
                        ],
                    )
                except HTTPException as exc:
                    assert exc.status_code == 400
                    assert "第 2 行" in exc.detail
                else:
                    raise AssertionError("Bulk import should fail when any row is invalid")

                count = (await db.execute(select(func.count()).select_from(Staff))).scalar_one()
                assert count == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())
