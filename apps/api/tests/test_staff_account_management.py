import asyncio
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.auth import login
from smart_badge_api.api.routes.staff import (
    activate_staff_account,
    create_staff,
    disable_staff_account,
    enable_staff_account,
    list_staff,
    reset_staff_account,
    update_staff,
)
from smart_badge_api.api.routes.account import get_managed_badges
from smart_badge_api.api.routes.positions import list_positions
from smart_badge_api.core.security import hash_password, verify_password
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import PositionProfile, Staff, StaffManagementRelation, User, VisitOrder, WecomTenant
from smart_badge_api.db.system_defaults import ensure_system_positions
from smart_badge_api.schemas.auth import LoginRequest
from smart_badge_api.schemas.staff import StaffCreate, StaffUpdate


def _make_request(ip: str = "127.0.0.1", path: str = "/api/v1/staff/account") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
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


def test_enable_staff_account_uses_employee_code_first() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                staff = Staff(
                    name="杜娟",
                    external_account="81019369",
                    phone="13800000000",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.commit()
                await db.refresh(staff)

                result = await enable_staff_account(staff.id, _make_request(), db=db, current_user=admin)

                assert result.created is True
                assert result.username == "81019369"
                assert result.temporary_password == "9369@Abcd"
                user = (await db.execute(select(User).where(User.staff_id == staff.id))).scalar_one()
                assert verify_password("9369@Abcd", user.hashed_password)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_managed_badges_returns_directly_managed_staff() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                manager = Staff(
                    id="manager",
                    name="主管",
                    external_account="M001",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                subordinate = Staff(
                    id="subordinate",
                    name="顾问",
                    external_account="S001",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                outside = Staff(
                    id="outside",
                    name="其他员工",
                    external_account="O001",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                user = User(
                    username="M001",
                    hashed_password="hashed",
                    display_name="主管",
                    staff_id=manager.id,
                    role="staff",
                    hospital_code="6101",
                    hospital_name="总院",
                    is_active=True,
                )
                relation = StaffManagementRelation(
                    hospital_code="6101",
                    manager_staff_id=manager.id,
                    subordinate_staff_id=subordinate.id,
                )
                db.add_all([manager, subordinate, outside, user, relation])
                await db.commit()

                badges = await get_managed_badges(response=None, db=db, user=user)

                assert len(badges) == 1
                assert badges[0].staff_id == subordinate.id
                assert badges[0].staff_name == "顾问"
                assert badges[0].bound is False
                assert badges[0].reason == "当前员工暂未绑定工牌"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_staff_create_and_update_resolve_wecom_corp_id_from_institution_code() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db, hospital_code=None)
                db.add_all(
                    [
                        WecomTenant(
                            name="长沙雅美",
                            default_hospital_code="6501",
                            default_hospital_name="长沙雅美",
                            corp_id="ww6501",
                            is_active=True,
                        ),
                        WecomTenant(
                            name="米兰柏羽总院",
                            default_hospital_code="6101",
                            default_hospital_name="米兰柏羽总院",
                            corp_id="ww6101",
                            is_active=True,
                        ),
                    ]
                )
                await db.commit()

                created = await create_staff(
                    StaffCreate(
                        name="陈巩",
                        external_account="80602823",
                        wecom_user_id="chengong",
                        hospital_code="6501",
                    ),
                    _make_request(path="/api/v1/staff"),
                    db=db,
                    current_user=admin,
                )

                assert created.wecom_corp_id == "ww6501"
                staff = (await db.execute(select(Staff).where(Staff.id == created.id))).scalar_one()
                assert staff.hospital_short_name == "长沙雅美"
                assert staff.wecom_corp_id == "ww6501"

                updated = await update_staff(
                    created.id,
                    StaffUpdate(hospital_code="6101"),
                    _make_request(path=f"/api/v1/staff/{created.id}"),
                    db=db,
                    current_user=admin,
                )

                assert updated.hospital_short_name == "米兰柏羽总院"
                assert updated.wecom_corp_id == "ww6101"
                await db.refresh(staff)
                assert staff.wecom_corp_id == "ww6101"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_create_staff_can_resolve_name_by_employee_code_from_visit_orders() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db, hospital_code=None)
                db.add_all(
                    [
                        WecomTenant(
                            name="米兰柏羽总院",
                            default_hospital_code="6101",
                            default_hospital_name="米兰柏羽总院",
                            corp_id="ww6101",
                            is_active=True,
                        ),
                        VisitOrder(
                            dzdh="AUTO-NAME-001",
                            dzseg="110",
                            sjrq="20260505",
                            jgbm="6101",
                            fzuer="90018888",
                            fzuer_long="自动补名",
                        ),
                    ]
                )
                await db.commit()

                with patch(
                    "smart_badge_api.api.routes.staff.lookup_dingtalk_user_by_job_number",
                    new=AsyncMock(return_value=None),
                ):
                    created = await create_staff(
                        StaffCreate(external_account="90018888", hospital_code="6101"),
                        _make_request(path="/api/v1/staff"),
                        db=db,
                        current_user=admin,
                    )

                assert created.name == "自动补名"
                assert created.external_account == "90018888"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_create_staff_resolves_name_by_employee_code_from_dingtalk_contacts_first() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db, hospital_code=None)
                db.add_all(
                    [
                        WecomTenant(
                            name="米兰柏羽总院",
                            default_hospital_code="6101",
                            default_hospital_name="米兰柏羽总院",
                            corp_id="ww6101",
                            is_active=True,
                        ),
                        VisitOrder(
                            dzdh="AUTO-NAME-002",
                            dzseg="110",
                            sjrq="20260505",
                            jgbm="6101",
                            fzuer="90018887",
                            fzuer_long="SAP姓名",
                        ),
                    ]
                )
                await db.commit()

                with patch(
                    "smart_badge_api.api.routes.staff.lookup_dingtalk_user_by_job_number",
                    new=AsyncMock(
                        return_value={
                            "job_number": "90018887",
                            "name": "钉钉姓名",
                            "mobile": "13900001111",
                        }
                    ),
                ):
                    created = await create_staff(
                        StaffCreate(external_account="90018887", hospital_code="6101"),
                        _make_request(path="/api/v1/staff"),
                        db=db,
                        current_user=admin,
                    )

                assert created.name == "钉钉姓名"
                assert created.phone == "13900001111"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_create_staff_without_name_requires_resolvable_employee_code() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db, hospital_code=None)
                db.add(
                    WecomTenant(
                        name="米兰柏羽总院",
                        default_hospital_code="6101",
                        default_hospital_name="米兰柏羽总院",
                        corp_id="ww6101",
                        is_active=True,
                    )
                )
                await db.commit()

                try:
                    with patch(
                        "smart_badge_api.api.routes.staff.lookup_dingtalk_user_by_job_number",
                        new=AsyncMock(return_value=None),
                    ):
                        await create_staff(
                            StaffCreate(external_account="NO_SUCH_CODE", hospital_code="6101"),
                            _make_request(path="/api/v1/staff"),
                            db=db,
                            current_user=admin,
                        )
                except HTTPException as exc:
                    assert exc.status_code == 400
                    assert "姓名" in exc.detail
                else:
                    raise AssertionError("Staff creation without a resolvable name should be rejected")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_hospital_admin_can_create_staff_and_hospital_admin_in_own_hospital() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                await ensure_system_positions(db)
                db.add(
                    WecomTenant(
                        name="长沙雅美",
                        default_hospital_code="6501",
                        default_hospital_name="长沙雅美",
                        corp_id="ww6501",
                        is_active=True,
                    )
                )
                await db.commit()
                admin = await _make_admin_user(db, role="hospital_admin", hospital_code="6501")
                staff_position = (
                    await db.execute(select(PositionProfile).where(PositionProfile.name == "普通员工"))
                ).scalar_one()
                hospital_admin_position = (
                    await db.execute(select(PositionProfile).where(PositionProfile.name == "机构管理员"))
                ).scalar_one()

                created_staff = await create_staff(
                    StaffCreate(
                        name="普通员工测试",
                        external_account="90010001",
                        hospital_code="6501",
                        position_id=staff_position.id,
                    ),
                    _make_request(path="/api/v1/staff"),
                    db=db,
                    current_user=admin,
                )
                created_admin = await create_staff(
                    StaffCreate(
                        name="机构管理员测试",
                        external_account="90010002",
                        hospital_code="6501",
                        position_id=hospital_admin_position.id,
                    ),
                    _make_request(path="/api/v1/staff"),
                    db=db,
                    current_user=admin,
                )

                assert created_staff.permission_role == "staff"
                assert created_staff.hospital_code == "6501"
                assert created_admin.permission_role == "hospital_admin"
                assert created_admin.hospital_code == "6501"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_hospital_admin_position_list_includes_staff_and_hospital_admin_roles() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                await ensure_system_positions(db)
                admin = await _make_admin_user(db, role="hospital_admin", hospital_code="6501")

                positions = await list_positions(
                    keyword=None,
                    position_type=None,
                    is_super_admin=None,
                    db=db,
                    current_user=admin,
                )

                assert {item.mapped_role for item in positions} == {"staff", "hospital_admin"}
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_create_staff_requires_employee_code() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db, hospital_code=None)
                db.add(
                    WecomTenant(
                        name="米兰柏羽总院",
                        default_hospital_code="6101",
                        default_hospital_name="米兰柏羽总院",
                        corp_id="ww6101",
                        is_active=True,
                    )
                )
                await db.commit()

                try:
                    await create_staff(
                        StaffCreate(name="缺员工编号", hospital_code="6101"),
                        _make_request(path="/api/v1/staff"),
                        db=db,
                        current_user=admin,
                    )
                except HTTPException as exc:
                    assert exc.status_code == 400
                    assert "员工编号" in exc.detail
                else:
                    raise AssertionError("Staff creation without employee code should be rejected")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_enable_staff_account_falls_back_to_phone_when_employee_code_missing() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                staff = Staff(
                    name="兰四秀",
                    phone="13800138000",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.commit()
                await db.refresh(staff)

                result = await enable_staff_account(staff.id, _make_request(), db=db, current_user=admin)

                assert result.username == "13800138000"
                assert result.temporary_password == "8000@Abcd"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_enable_staff_account_rejects_staff_without_employee_code_or_phone() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                staff = Staff(
                    name="无账号来源员工",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.commit()
                await db.refresh(staff)

                try:
                    await enable_staff_account(staff.id, _make_request(), db=db, current_user=admin)
                except HTTPException as exc:
                    assert exc.status_code == 400
                    assert "工号或手机号" in exc.detail
                else:
                    raise AssertionError("Staff without employee code or phone should be rejected")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_enable_staff_account_normalizes_legacy_wecom_prefixed_phone_username() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                staff = Staff(
                    name="兰四秀",
                    phone="13800138000",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.flush()

                user = User(
                    username="wecom_13800138000",
                    hashed_password=hash_password("legacy-pass"),
                    display_name="兰四秀",
                    staff_id=staff.id,
                    role="staff",
                    is_active=True,
                )
                db.add(user)
                await db.commit()

                result = await enable_staff_account(staff.id, _make_request(), db=db, current_user=admin)

                assert result.created is False
                assert result.username == "13800138000"
                normalized_user = (await db.execute(select(User).where(User.staff_id == staff.id))).scalar_one()
                assert normalized_user.username == "13800138000"
                assert verify_password("legacy-pass", normalized_user.hashed_password)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_reset_disable_and_activate_staff_account() -> None:
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

                await enable_staff_account(staff.id, _make_request(), db=db, current_user=admin)

                reset_result = await reset_staff_account(staff.id, _make_request(), db=db, current_user=admin)
                assert reset_result.temporary_password == "0995@Abcd"

                disable_result = await disable_staff_account(staff.id, _make_request(), db=db, current_user=admin)
                assert disable_result.is_active is False

                activate_result = await activate_staff_account(staff.id, _make_request(), db=db, current_user=admin)
                assert activate_result.is_active is True
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_enable_staff_account_rejects_conflicting_username() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                staff = Staff(
                    name="冲突员工",
                    external_account="81019369",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                db.add_all(
                    [
                        staff,
                        User(
                            username="81019369",
                            hashed_password=hash_password("existing123"),
                            display_name="已存在账号",
                            is_active=True,
                        ),
                    ]
                )
                await db.commit()
                await db.refresh(staff)

                try:
                    await enable_staff_account(staff.id, _make_request(), db=db, current_user=admin)
                except HTTPException as exc:
                    assert exc.status_code == 409
                    assert "已被其他员工占用" in exc.detail
                else:
                    raise AssertionError("Conflicting username should be rejected")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_disabled_staff_account_cannot_login_until_reactivated() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                staff = Staff(
                    name="可登录员工",
                    external_account="86000995",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.commit()
                await db.refresh(staff)

                await enable_staff_account(staff.id, _make_request(), db=db, current_user=admin)
                await disable_staff_account(staff.id, _make_request(), db=db, current_user=admin)

                try:
                    await login(
                        LoginRequest(username="86000995", password="0995@Abcd"),
                        _make_request(path="/api/v1/auth/login"),
                        db=db,
                    )
                except HTTPException as exc:
                    assert exc.status_code == 403
                    assert "账号已禁用" in exc.detail
                else:
                    raise AssertionError("Disabled account should not login")

                await activate_staff_account(staff.id, _make_request(), db=db, current_user=admin)
                tokens = await login(
                    LoginRequest(username="86000995", password="0995@Abcd"),
                    _make_request(path="/api/v1/auth/login"),
                    db=db,
                )

                assert tokens.access_token
                assert tokens.refresh_token
                user = (await db.execute(select(User).where(User.staff_id == staff.id))).scalar_one()
                assert user.last_login_at is not None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_list_staff_supports_account_status_filter() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                not_opened = Staff(
                    name="账号状态筛选-未开通",
                    external_account="91000001",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                active_staff = Staff(
                    name="账号状态筛选-正常",
                    external_account="91000002",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                disabled_staff = Staff(
                    name="账号状态筛选-已停用",
                    external_account="91000003",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                db.add_all([not_opened, active_staff, disabled_staff])
                await db.commit()

                await enable_staff_account(active_staff.id, _make_request(), db=db, current_user=admin)
                await enable_staff_account(disabled_staff.id, _make_request(), db=db, current_user=admin)
                await disable_staff_account(disabled_staff.id, _make_request(), db=db, current_user=admin)

                not_opened_page = await list_staff(
                    keyword="账号状态筛选",
                    account_status="not_opened",
                    db=db,
                    current_user=admin,
                )
                assert [item.name for item in not_opened_page.items] == ["账号状态筛选-未开通"]

                active_page = await list_staff(
                    keyword="账号状态筛选",
                    account_status="active",
                    db=db,
                    current_user=admin,
                )
                assert [item.name for item in active_page.items] == ["账号状态筛选-正常"]

                disabled_page = await list_staff(
                    keyword="账号状态筛选",
                    account_status="disabled",
                    db=db,
                    current_user=admin,
                )
                assert [item.name for item in disabled_page.items] == ["账号状态筛选-已停用"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_regular_staff_cannot_manage_other_staff_accounts() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                target = Staff(
                    name="目标员工",
                    external_account="81047230",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                actor_staff = Staff(
                    name="普通员工",
                    external_account="81000001",
                    hospital_code="6101",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                db.add_all([target, actor_staff])
                await db.commit()
                await db.refresh(target)
                await db.refresh(actor_staff)

                actor = User(
                    username="81000001",
                    hashed_password="hashed",
                    display_name="普通员工",
                    staff_id=actor_staff.id,
                    role="staff",
                    hospital_code="6101",
                    hospital_name="总院",
                    is_active=True,
                )
                db.add(actor)
                await db.commit()
                await db.refresh(actor)

                try:
                    await enable_staff_account(target.id, _make_request(), db=db, current_user=actor)
                except HTTPException as exc:
                    assert exc.status_code == 404
                else:
                    raise AssertionError("Regular staff should not manage other accounts")
        finally:
            await engine.dispose()

    asyncio.run(scenario())
