import asyncio
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.account import (
    change_account_password,
    get_managed_badges,
    get_account_profile,
    get_my_badge,
    start_my_badge_recording,
    stop_my_badge_recording,
    update_account_profile,
)
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import AuditLog, Device, PositionProfile, Staff, StaffManagementRelation, User
from smart_badge_api.core.security import hash_password, verify_password
from smart_badge_api.schemas.profile import AccountProfileUpdate, ChangePasswordRequest


def _make_request(ip: str = "127.0.0.1") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/account",
        "headers": [],
        "client": (ip, 8000),
    }
    return Request(scope)


def test_get_account_profile_includes_recent_activities() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                user = User(username="admin", display_name="管理员", hashed_password=hash_password("admin123"))
                db.add(user)
                await db.commit()
                await db.refresh(user)

                db.add_all(
                    [
                        AuditLog(
                            operator_name="管理员",
                            ip_address="127.0.0.1",
                            module_name="登录系统",
                            action_name="账号密码登录",
                            content="账号密码登录",
                        ),
                        AuditLog(
                            operator_name="admin",
                            ip_address="127.0.0.1",
                            module_name="个人中心",
                            action_name="更新资料",
                            content="更新显示名称为 管理员",
                        ),
                    ]
                )
                await db.commit()

                profile = await get_account_profile(db=db, user=user)

                assert profile.username == "admin"
                assert profile.activity_count == 2
                assert len(profile.recent_activities) == 2
                assert profile.last_activity_at is not None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_update_account_profile_persists_name_and_writes_audit_log() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                user = User(username="viewer", display_name="旧名字", hashed_password=hash_password("viewer123"))
                db.add(user)
                await db.commit()
                await db.refresh(user)

                result = await update_account_profile(
                    AccountProfileUpdate(display_name=" 新名字 "),
                    _make_request(),
                    db=db,
                    user=user,
                )

                assert result.display_name == "新名字"

                refreshed = await db.get(User, user.id)
                assert refreshed is not None
                assert refreshed.display_name == "新名字"

                logs = (await db.execute(AuditLog.__table__.select())).all()
                assert len(logs) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_change_account_password_updates_hash_and_rejects_wrong_password() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                user = User(username="manager", display_name="经理", hashed_password=hash_password("manager123"))
                db.add(user)
                await db.commit()
                await db.refresh(user)

                try:
                    await change_account_password(
                        ChangePasswordRequest(current_password="wrong", new_password="newpass123"),
                        _make_request(),
                        db=db,
                        user=user,
                    )
                except HTTPException as exc:
                    assert exc.status_code == 400
                    assert "当前密码错误" in exc.detail
                else:
                    raise AssertionError("Wrong current password should be rejected")

                result = await change_account_password(
                    ChangePasswordRequest(current_password="manager123", new_password="newpass123"),
                    _make_request("10.0.0.8"),
                    db=db,
                    user=user,
                )

                assert result.message == "密码已更新"

                refreshed = await db.get(User, user.id)
                assert refreshed is not None
                assert verify_password("newpass123", refreshed.hashed_password)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_get_my_badge_returns_unbound_reason_when_account_has_no_staff() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                user = User(username="viewer", display_name="普通员工", hashed_password=hash_password("viewer123"))
                db.add(user)
                await db.commit()
                await db.refresh(user)

                result = await get_my_badge(db=db, user=user)

                assert result.bound is False
                assert result.reason == "当前账号未关联系统人员"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_get_managed_badges_uses_management_scope_and_filters_higher_permission_staff() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                manager = Staff(id="system-manager", name="System manager", permission_role="system_admin", is_active=True)
                managed = Staff(id="normal-managed", name="Normal managed", permission_role="staff", is_active=True)
                super_admin = Staff(id="super-admin-staff", name="Super admin", permission_role="super_admin", is_active=True)
                user = User(
                    username="system-admin",
                    display_name="System admin",
                    hashed_password=hash_password("admin123"),
                    staff_id=manager.id,
                    role="system_admin",
                    is_active=True,
                )
                db.add_all([
                    manager,
                    managed,
                    super_admin,
                    user,
                    StaffManagementRelation(
                        hospital_code="6101",
                        manager_staff_id=manager.id,
                        subordinate_staff_id=managed.id,
                    ),
                    StaffManagementRelation(
                        hospital_code="6101",
                        manager_staff_id=manager.id,
                        subordinate_staff_id=super_admin.id,
                    ),
                ])
                await db.commit()

                result = await get_managed_badges(db=db, user=user)

                assert [item.staff_id for item in result] == [managed.id]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_get_managed_badges_batches_iot_status_queries() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                manager = Staff(id="manager", name="Manager", permission_role="super_admin", hospital_code="6501", is_active=True)
                staff_a = Staff(id="staff_a", name="Staff A", permission_role="staff", hospital_code="6501", is_active=True)
                staff_b = Staff(id="staff_b", name="Staff B", permission_role="staff", hospital_code="6501", is_active=True)
                user = User(
                    username="manager",
                    display_name="Manager",
                    hashed_password=hash_password("admin123"),
                    staff_id=manager.id,
                    role="super_admin",
                    is_active=True,
                )
                db.add_all([
                    manager,
                    staff_a,
                    staff_b,
                    user,
                    Device(
                        name="A",
                        device_code="IOT-A",
                        staff_id=staff_a.id,
                        hospital_code="6501",
                        status="offline",
                        is_active=True,
                    ),
                    Device(
                        name="B",
                        device_code="IOT-B",
                        staff_id=staff_b.id,
                        hospital_code="6501",
                        status="offline",
                        is_active=True,
                    ),
                    StaffManagementRelation(
                        hospital_code="6501",
                        manager_staff_id=manager.id,
                        subordinate_staff_id=staff_a.id,
                    ),
                    StaffManagementRelation(
                        hospital_code="6501",
                        manager_staff_id=manager.id,
                        subordinate_staff_id=staff_b.id,
                    ),
                ])
                await db.commit()

                with patch(
                    "smart_badge_api.api.routes.account.iot_list_devices",
                    AsyncMock(return_value=[
                        {
                            "sn": "IOT-A",
                            "name": "A",
                            "status": "online",
                            "battery": 88,
                            "recordingStartTime": None,
                        },
                        {
                            "sn": "IOT-B",
                            "name": "B",
                            "status": "recording",
                            "battery": 55,
                            "recordingStartTime": 1778196000000,
                        },
                    ]),
                ) as mocked_iot_list:
                    result = await get_managed_badges(db=db, user=user)

                mocked_iot_list.assert_awaited_once()
                assert [item.staff_id for item in result] == [staff_a.id, staff_b.id]
                assert result[0].online is True
                assert result[0].battery_level == 88
                assert result[1].is_recording is True
                assert result[1].battery_level == 55
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_get_my_badge_auto_links_unique_staff_by_display_name() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="Tero",
                    wecom_user_id="15608171708",
                    permission_role="super_admin",
                    is_active=True,
                )
                db.add(staff)
                await db.flush()
                db.add(
                    Device(
                        name="Tero工牌",
                        device_code="SSYX41022727",
                        staff_id=staff.id,
                        status="offline",
                        is_active=True,
                    )
                )
                user = User(
                    username="admin",
                    display_name="Tero",
                    hashed_password=hash_password("admin123"),
                    role="super_admin",
                )
                db.add(user)
                await db.commit()
                await db.refresh(user)

                with (
                    patch(
                        "smart_badge_api.api.routes.account.dvi_list_devices",
                        AsyncMock(return_value={"result": [{"sn": "SSYX41022727", "name": "Tero工牌"}]}),
                    ),
                    patch(
                        "smart_badge_api.api.routes.account.dvi_query_device_status",
                        AsyncMock(return_value={"result": [{"sn": "SSYX41022727", "status": "offline"}]}),
                    ),
                ):
                    result = await get_my_badge(db=db, user=user)

                assert result.bound is True
                assert result.staff_name == "Tero"
                assert result.device_code == "SSYX41022727"

                refreshed_user = await db.get(User, user.id)
                assert refreshed_user is not None
                assert refreshed_user.staff_id == staff.id
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_get_my_badge_returns_remote_status_and_recording_capability() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                position = PositionProfile(name="咨询师", mapped_role="staff")
                db.add(position)
                await db.flush()
                staff = Staff(
                    name="钟露",
                    external_account="86000995",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                    position_id=position.id,
                )
                db.add(staff)
                await db.flush()
                device = Device(
                    name="本地工牌",
                    device_code="SSYX41022508",
                    staff_id=staff.id,
                    status="offline",
                    is_active=True,
                )
                user = User(
                    username="86000995",
                    display_name="钟露",
                    hashed_password=hash_password("viewer123"),
                    staff_id=staff.id,
                    role="staff",
                )
                db.add(device)
                db.add(user)
                await db.commit()
                await db.refresh(user)

                with (
                    patch(
                        "smart_badge_api.api.routes.account.dvi_list_devices",
                        AsyncMock(
                            return_value={
                                "result": [
                                    {
                                        "sn": "SSYX41022508",
                                        "name": "八号工牌",
                                        "teamCode": "team-008",
                                        "userId": "dt-user-008",
                                        "status": "online",
                                    }
                                ]
                            }
                        ),
                    ),
                    patch(
                        "smart_badge_api.api.routes.account.dvi_query_device_status",
                        AsyncMock(
                            return_value={
                                "result": [
                                    {
                                        "sn": "SSYX41022508",
                                        "status": {"value": "online"},
                                        "battery": {"value": 87},
                                        "recordingStartTime": {"value": 1712620800000},
                                    }
                                ]
                            }
                        ),
                    ),
                ):
                    result = await get_my_badge(db=db, user=user)

                assert result.bound is True
                assert result.device_code == "SSYX41022508"
                assert result.device_name == "八号工牌"
                assert result.staff_name == "钟露"
                assert result.position_name == "咨询师"
                assert result.online is True
                assert result.battery_level == 87
                assert result.team_code == "team-008"
                assert result.user_id == "dt-user-008"
                assert result.can_control_recording is True
                assert result.is_recording is True
                assert result.recording_started_at == "2024-04-09T00:00:00+00:00"

                refreshed_device = await db.get(Device, device.id)
                assert refreshed_device is not None
                assert refreshed_device.name == "八号工牌"
                assert refreshed_device.battery_level == 87
                assert refreshed_device.status == "online"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_get_my_badge_uses_iot_for_changsha_yamei() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="长沙顾问",
                    external_account="65010001",
                    hospital_code="6501",
                    hospital_short_name="长沙雅美",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.flush()
                device = Device(
                    name="本地工牌",
                    device_code="SSYX51049784",
                    staff_id=staff.id,
                    status="offline",
                    is_active=True,
                )
                user = User(
                    username="65010001",
                    display_name="长沙顾问",
                    hashed_password=hash_password("viewer123"),
                    staff_id=staff.id,
                    role="staff",
                )
                db.add(device)
                db.add(user)
                await db.commit()
                await db.refresh(user)

                with (
                    patch("smart_badge_api.api.routes.account.dvi_list_devices", AsyncMock()) as dvi_list,
                    patch("smart_badge_api.api.routes.account.dvi_query_device_status", AsyncMock()) as dvi_status,
                    patch(
                        "smart_badge_api.api.routes.account.iot_list_devices",
                        AsyncMock(
                            return_value=[
                                {
                                    "sn": "SSYX51049784",
                                    "name": "长沙IOT工牌",
                                    "status": {"value": "online"},
                                    "battery": {"value": 81},
                                    "remoteProvider": "iot",
                                }
                            ]
                        ),
                    ) as iot_list,
                    patch(
                        "smart_badge_api.api.routes.account.iot_query_device_statuses",
                        AsyncMock(
                            return_value=[
                                {
                                    "sn": "SSYX51049784",
                                    "status": {"value": "recording"},
                                    "battery": {"value": 82},
                                    "recordingStartTime": {"value": 1712620800000},
                                    "remoteProvider": "iot",
                                }
                            ]
                        ),
                    ) as iot_status,
                ):
                    result = await get_my_badge(db=db, user=user)

                dvi_list.assert_not_awaited()
                dvi_status.assert_not_awaited()
                iot_list.assert_awaited_once_with(device_no="SSYX51049784")
                iot_status.assert_awaited_once_with(["SSYX51049784"])

                assert result.bound is True
                assert result.device_code == "SSYX51049784"
                assert result.device_name == "长沙IOT工牌"
                assert result.online is True
                assert result.battery_level == 82
                assert result.can_control_recording is True
                assert result.team_code is None
                assert result.user_id is None
                assert result.is_recording is True

                refreshed_device = await db.get(Device, device.id)
                assert refreshed_device is not None
                assert refreshed_device.hospital_code == "6501"
                assert refreshed_device.hospital_short_name == "长沙雅美"
                assert refreshed_device.status == "online"
                assert refreshed_device.battery_level == 82
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_get_my_badge_treats_idle_status_as_online() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="钟露",
                    external_account="86000995",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.flush()
                device = Device(
                    name="本地工牌",
                    device_code="SSYX41022508",
                    staff_id=staff.id,
                    status="offline",
                    is_active=True,
                )
                user = User(
                    username="86000995",
                    display_name="钟露",
                    hashed_password=hash_password("viewer123"),
                    staff_id=staff.id,
                    role="staff",
                )
                db.add(device)
                db.add(user)
                await db.commit()
                await db.refresh(user)

                with (
                    patch(
                        "smart_badge_api.api.routes.account.dvi_list_devices",
                        AsyncMock(
                            return_value={
                                "result": [
                                    {
                                        "sn": "SSYX41022508",
                                        "teamCode": "team-008",
                                        "userId": "dt-user-008",
                                    }
                                ]
                            }
                        ),
                    ),
                    patch(
                        "smart_badge_api.api.routes.account.dvi_query_device_status",
                        AsyncMock(
                            return_value={
                                "result": [
                                    {
                                        "sn": "SSYX41022508",
                                        "status": {"value": "idle"},
                                        "battery": {"value": 39},
                                        "recordingStartTime": {},
                                    }
                                ]
                            }
                        ),
                    ),
                ):
                    result = await get_my_badge(db=db, user=user)

                assert result.bound is True
                assert result.online is True
                assert result.status == "online"
                assert result.battery_level == 39
                assert result.is_recording is False

                refreshed_device = await db.get(Device, device.id)
                assert refreshed_device is not None
                assert refreshed_device.status == "online"
                assert refreshed_device.battery_level == 39
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_get_my_badge_prefers_realtime_idle_status_over_stale_list_recording_time() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="钟露",
                    external_account="86000995",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.flush()
                device = Device(
                    name="本地工牌",
                    device_code="SSYX41022508",
                    staff_id=staff.id,
                    status="online",
                    is_active=True,
                )
                user = User(
                    username="86000995",
                    display_name="钟露",
                    hashed_password=hash_password("viewer123"),
                    staff_id=staff.id,
                    role="staff",
                )
                db.add(device)
                db.add(user)
                await db.commit()
                await db.refresh(user)

                with (
                    patch(
                        "smart_badge_api.api.routes.account.dvi_list_devices",
                        AsyncMock(
                            return_value={
                                "result": [
                                    {
                                        "sn": "SSYX41022508",
                                        "teamCode": "team-008",
                                        "userId": "dt-user-008",
                                        "status": "recording",
                                        "recordingStartTime": 1712620800000,
                                    }
                                ]
                            }
                        ),
                    ),
                    patch(
                        "smart_badge_api.api.routes.account.dvi_query_device_status",
                        AsyncMock(
                            return_value={
                                "result": [
                                    {
                                        "sn": "SSYX41022508",
                                        "status": {"value": "idle"},
                                        "battery": {"value": 61},
                                    }
                                ]
                            }
                        ),
                    ),
                ):
                    result = await get_my_badge(db=db, user=user)

                assert result.bound is True
                assert result.online is True
                assert result.battery_level == 61
                assert result.is_recording is False
                assert result.recording_started_at is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_start_my_badge_recording_uses_current_user_bound_badge() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="赵衡",
                    external_account="81021570",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.flush()
                db.add(
                    Device(
                        name="一号工牌",
                        device_code="SN001",
                        staff_id=staff.id,
                        status="online",
                        is_active=True,
                    )
                )
                user = User(
                    username="81021570",
                    display_name="赵衡",
                    hashed_password=hash_password("viewer123"),
                    staff_id=staff.id,
                    role="staff",
                )
                db.add(user)
                await db.commit()
                await db.refresh(user)

                with (
                    patch(
                        "smart_badge_api.api.routes.account.dvi_list_devices",
                        AsyncMock(
                            return_value={
                                "result": [
                                    {
                                        "sn": "SN001",
                                        "teamCode": "team-001",
                                        "userId": "dt-user-001",
                                    }
                                ]
                            }
                        ),
                    ),
                    patch(
                        "smart_badge_api.api.routes.account.dvi_query_device_status",
                        AsyncMock(return_value={"result": [{"sn": "SN001", "status": "online"}]}),
                    ),
                    patch(
                        "smart_badge_api.api.routes.account.dvi_control_recording",
                        AsyncMock(return_value={"success": True}),
                    ) as control_mock,
                ):
                    result = await start_my_badge_recording(
                        request=_make_request("10.10.0.8"),
                        db=db,
                        user=user,
                    )

                assert result.message == "录音已启动"
                control_mock.assert_awaited_once_with(
                    action="start",
                    team_code="team-001",
                    user_id="dt-user-001",
                )
                logs = (await db.execute(AuditLog.__table__.select())).all()
                assert len(logs) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_start_my_badge_recording_uses_iot_for_changsha_yamei() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="长沙顾问",
                    external_account="65010001",
                    hospital_code="6501",
                    hospital_short_name="长沙雅美",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.flush()
                db.add(
                    Device(
                        name="长沙工牌",
                        device_code="SSYX51049784",
                        staff_id=staff.id,
                        status="online",
                        is_active=True,
                    )
                )
                user = User(
                    username="65010001",
                    display_name="长沙顾问",
                    hashed_password=hash_password("viewer123"),
                    staff_id=staff.id,
                    role="staff",
                )
                db.add(user)
                await db.commit()
                await db.refresh(user)

                with (
                    patch("smart_badge_api.api.routes.account.dvi_list_devices", AsyncMock()) as dvi_list,
                    patch("smart_badge_api.api.routes.account.dvi_query_device_status", AsyncMock()) as dvi_status,
                    patch("smart_badge_api.api.routes.account.dvi_control_recording", AsyncMock()) as dvi_control,
                    patch(
                        "smart_badge_api.api.routes.account.iot_list_devices",
                        AsyncMock(
                            return_value=[
                                {
                                    "sn": "SSYX51049784",
                                    "name": "长沙工牌",
                                    "status": {"value": "online"},
                                    "battery": {"value": 88},
                                    "remoteProvider": "iot",
                                }
                            ]
                        ),
                    ),
                    patch(
                        "smart_badge_api.api.routes.account.iot_query_device_statuses",
                        AsyncMock(
                            return_value=[
                                {
                                    "sn": "SSYX51049784",
                                    "status": {"value": "online"},
                                    "battery": {"value": 88},
                                    "remoteProvider": "iot",
                                }
                            ]
                        ),
                    ),
                    patch(
                        "smart_badge_api.api.routes.account.iot_control_recording",
                        AsyncMock(return_value={"success": True}),
                    ) as iot_control,
                ):
                    result = await start_my_badge_recording(
                        request=_make_request("10.10.0.8"),
                        db=db,
                        user=user,
                    )

                assert result.message == "录音已启动"
                dvi_list.assert_not_awaited()
                dvi_status.assert_not_awaited()
                dvi_control.assert_not_awaited()
                iot_control.assert_awaited_once_with(action="start", device_no="SSYX51049784")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_stop_my_badge_recording_rejects_when_remote_binding_missing() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="兰四秀",
                    external_account="81047230",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.flush()
                db.add(
                    Device(
                        name="二号工牌",
                        device_code="SN002",
                        staff_id=staff.id,
                        status="offline",
                        is_active=True,
                    )
                )
                user = User(
                    username="81047230",
                    display_name="兰四秀",
                    hashed_password=hash_password("viewer123"),
                    staff_id=staff.id,
                    role="staff",
                )
                db.add(user)
                await db.commit()
                await db.refresh(user)

                with (
                    patch(
                        "smart_badge_api.api.routes.account.dvi_list_devices",
                        AsyncMock(return_value={"result": [{"sn": "SN002", "name": "二号工牌"}]}),
                    ),
                    patch(
                        "smart_badge_api.api.routes.account.dvi_query_device_status",
                        AsyncMock(return_value={"result": [{"sn": "SN002", "status": "offline"}]}),
                    ),
                ):
                    try:
                        await stop_my_badge_recording(
                            request=_make_request("10.10.0.9"),
                            db=db,
                            user=user,
                        )
                    except HTTPException as exc:
                        assert exc.status_code == 400
                        assert "尚未完成钉钉侧绑定" in exc.detail
                    else:
                        raise AssertionError("Expected stop_my_badge_recording to reject missing remote binding")
        finally:
            await engine.dispose()

    asyncio.run(scenario())
