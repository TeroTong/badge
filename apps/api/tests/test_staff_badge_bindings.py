import asyncio

from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.staff import (
    create_staff,
    list_staff_hospital_options,
    list_staff,
    list_staff_badge_binding_candidates,
    update_staff,
    update_staff_badge_binding,
)
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import Device, Staff, User, WecomTenant
from smart_badge_api.schemas.staff import StaffBadgeBindingUpdate, StaffCreate, StaffUpdate


def _make_request(ip: str = "127.0.0.1", path: str = "/api/v1/staff/badge-binding") -> Request:
    scope = {
        "type": "http",
        "method": "PUT",
        "path": path,
        "headers": [],
        "client": (ip, 8000),
    }
    return Request(scope)


async def _make_admin_user(db, *, role: str = "system_admin", hospital_code: str | None = "6101") -> User:
    user = User(
        username=f"{role}_user",
        hashed_password="hashed",
        display_name="管理员",
        role=role,
        hospital_code=hospital_code,
        hospital_name="测试医院" if hospital_code else None,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _add_institution(db, *, code: str = "6101", name: str = "米兰柏羽总院") -> WecomTenant:
    tenant = WecomTenant(
        name=name,
        host=None,
        corp_id=None,
        agent_id=None,
        agent_secret=None,
        frontend_url=None,
        default_hospital_code=code,
        default_hospital_name=None,
        is_default=True,
        is_active=True,
    )
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


def test_list_staff_keyword_search_no_longer_uses_dingtalk_user_id() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                db.add_all(
                    [
                        Staff(
                            name="杜娟",
                            external_account="81019369",
                            wecom_user_id="dujuan_wecom",
                            hospital_code="6101",
                            hospital_short_name="总院",
                            permission_role="staff",
                            is_active=True,
                        ),
                        Staff(
                            name="兰四秀",
                            external_account="81047230",
                            wecom_user_id="lansixiu_wecom",
                            hospital_code="6101",
                            hospital_short_name="总院",
                            permission_role="staff",
                            is_active=True,
                        ),
                    ]
                )
                await db.commit()

                result = await list_staff(
                    keyword="dujuan_wecom",
                    page=1,
                    page_size=20,
                    db=db,
                    current_user=admin,
                )

                assert result.total == 1
                assert result.items[0].name == "杜娟"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_list_staff_can_filter_by_hospital_code() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                db.add_all(
                    [
                        Staff(
                            name="总院顾问",
                            external_account="81000001",
                            hospital_code="6101",
                            hospital_short_name="总院",
                            permission_role="staff",
                            is_active=True,
                        ),
                        Staff(
                            name="分院顾问",
                            external_account="82000001",
                            hospital_code="6201",
                            hospital_short_name="分院",
                            permission_role="staff",
                            is_active=True,
                        ),
                    ]
                )
                await db.commit()

                result = await list_staff(
                    hospital_code="6201",
                    page=1,
                    page_size=20,
                    db=db,
                    current_user=admin,
                )

                assert result.total == 1
                assert result.items[0].name == "分院顾问"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_list_staff_hospital_options_respects_scope() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                hospital_admin = await _make_admin_user(db, role="hospital_admin", hospital_code="6101")
                await _add_institution(db, code="6101", name="米兰柏羽总院")
                await _add_institution(db, code="6201", name="外院")
                db.add_all(
                    [
                        Staff(
                            name="总院顾问",
                            external_account="81000001",
                            hospital_code="6101",
                            hospital_short_name="总院",
                            permission_role="staff",
                            is_active=True,
                        ),
                        Staff(
                            name="分院顾问",
                            external_account="82000001",
                            hospital_code="6201",
                            hospital_short_name="分院",
                            permission_role="staff",
                            is_active=True,
                        ),
                    ]
                )
                await db.commit()

                options = await list_staff_hospital_options(db=db, current_user=hospital_admin)

                assert [(item.hospital_code, item.hospital_name) for item in options] == [("6101", "米兰柏羽总院")]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_list_staff_badge_binding_candidates_respects_scope_and_active_flag() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                hospital_admin = await _make_admin_user(db, role="hospital_admin", hospital_code="6101")
                db.add_all(
                    [
                        Staff(
                            name="院内启用人员",
                            external_account="81000001",
                            hospital_code="6101",
                            hospital_short_name="总院",
                            permission_role="staff",
                            is_active=True,
                        ),
                        Staff(
                            name="院内停用人员",
                            external_account="81000002",
                            hospital_code="6101",
                            hospital_short_name="总院",
                            permission_role="staff",
                            is_active=False,
                        ),
                        Staff(
                            name="外院人员",
                            external_account="81000003",
                            hospital_code="6201",
                            hospital_short_name="分院",
                            permission_role="staff",
                            is_active=True,
                        ),
                    ]
                )
                await db.commit()

                active_only = await list_staff_badge_binding_candidates(
                    include_inactive=False,
                    db=db,
                    current_user=hospital_admin,
                )
                assert [item.external_account for item in active_only] == ["81000001"]

                including_inactive = await list_staff_badge_binding_candidates(
                    include_inactive=True,
                    db=db,
                    current_user=hospital_admin,
                )
                assert {item.external_account for item in including_inactive} == {"81000001", "81000002"}
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_staff_badge_binding_endpoint_is_disabled() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                staff = Staff(
                    name="钟露",
                    external_account="86000995",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.commit()
                await db.refresh(staff)

                try:
                    await update_staff_badge_binding(
                        staff.id,
                        StaffBadgeBindingUpdate(device_code="SN001"),
                        current_user=admin,
                    )
                    assert False, "expected disabled staff badge binding endpoint"
                except HTTPException as exc:
                    assert exc.status_code == 410
                    assert "朗姿工牌" in str(exc.detail)

        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_create_staff_ignores_badge_id_from_profile_payload() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                await _add_institution(db, code="6101", name="米兰柏羽总院")

                result = await create_staff(
                    StaffCreate.model_validate(
                        {
                            "name": "顾问A",
                            "external_account": "81010001",
                            "hospital_code": "6101",
                            "hospital_short_name": "总院",
                            "badge_id": "SN100",
                            "is_active": True,
                        }
                    ),
                    _make_request(path="/api/v1/staff"),
                    db=db,
                    current_user=admin,
                )

                assert result.badge_id is None
                assert result.hospital_short_name == "米兰柏羽总院"
                device = (await db.execute(select(Device).where(Device.device_code == "SN100"))).scalar_one_or_none()
                assert device is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_update_staff_ignores_badge_id_from_profile_payload() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                await _add_institution(db, code="6101", name="米兰柏羽总院")
                staff_a = Staff(
                    name="顾问A",
                    external_account="81010001",
                    badge_id="SN200",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                staff_b = Staff(
                    name="顾问B",
                    external_account="81010002",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                db.add_all([staff_a, staff_b])
                await db.flush()
                db.add(Device(name="二号工牌", device_code="SN200", staff_id=staff_a.id, is_active=True))
                await db.commit()
                await db.refresh(staff_a)
                await db.refresh(staff_b)

                result = await update_staff(
                    staff_b.id,
                    StaffUpdate.model_validate({"badge_id": "SN200", "phone": "13800000000"}),
                    _make_request(path=f"/api/v1/staff/{staff_b.id}"),
                    db=db,
                    current_user=admin,
                )

                assert result.phone == "13800000000"
                assert result.badge_id is None
                assert result.hospital_short_name == "米兰柏羽总院"
                await db.refresh(staff_a)
                await db.refresh(staff_b)
                assert staff_a.badge_id == "SN200"
                assert staff_b.badge_id is None
                device = (await db.execute(select(Device).where(Device.device_code == "SN200"))).scalar_one()
                assert device.staff_id == staff_a.id
        finally:
            await engine.dispose()

    asyncio.run(scenario())
