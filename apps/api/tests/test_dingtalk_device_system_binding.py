import asyncio
from datetime import timezone
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.dingtalk import (
    _attach_archive_recording_bindings,
    _build_archive_recording_summary,
    BindDeviceRequest,
    SnListRequest,
    SystemBindDeviceRequest,
    SystemUnbindDeviceRequest,
    bind_device,
    bind_system_device,
    get_device_status,
    list_devices,
    unbind_device,
    unbind_system_device,
)
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import Customer, Device, DeviceStaffBinding, Recording, RecordingVisitLink, Staff, User, Visit
from smart_badge_api.device_binding import EARLIEST_BINDING_AT
from smart_badge_api.dingtalk import DingTalkApiError


async def _make_user(
    db,
    *,
    username: str = "staff_login",
    staff_id: str | None = None,
    role: str = "system_admin",
    hospital_code: str | None = None,
) -> User:
    user = User(
        username=username,
        hashed_password="hashed",
        display_name="系统账号",
        role=role,
        staff_id=staff_id,
        hospital_code=hospital_code,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def test_list_devices_syncs_remote_device_cache_and_preserves_system_binding() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="杜娟",
                    external_account="81019369",
                    hospital_short_name="总院",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.commit()
                await db.refresh(staff)
                current_user = await _make_user(db, staff_id=staff.id)
                db.add(
                    Device(
                        name="旧设备名",
                        device_code="SN001",
                        staff_id=staff.id,
                        status="offline",
                        is_active=True,
                    )
                )
                await db.commit()

                with patch(
                    "smart_badge_api.api.routes.dingtalk.dvi_list_devices",
                    AsyncMock(
                        return_value={
                            "result": [
                                {
                                    "sn": "SN001",
                                    "name": "一号工牌",
                                    "teamCode": "team-001",
                                    "userId": "dt_user_001",
                                    "status": "online",
                                }
                            ]
                        }
                    ),
                ):
                    payload = await list_devices(db=db, current_user=current_user)

                assert payload["totalCount"] == 1
                row = payload["result"][0]
                assert row["sn"] == "SN001"
                assert row["systemBinding"]["staffId"] == staff.id
                assert row["systemBinding"]["staffName"] == "杜娟"
                assert row["systemBinding"]["accountUsername"] == "staff_login"

                device = (
                    await db.execute(select(Device).where(Device.device_code == "SN001"))
                ).scalar_one()
                assert device.name == "一号工牌"
                assert device.staff_id == staff.id
                assert device.dingtalk_team_code == "team-001"
                assert device.dingtalk_user_id == "dt_user_001"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_list_devices_uses_iot_for_changsha_yamei() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                current_user = await _make_user(db, role="hospital_admin", hospital_code="6501")

                with (
                    patch("smart_badge_api.api.routes.dingtalk.dvi_list_devices", AsyncMock()) as dvi_list,
                    patch(
                        "smart_badge_api.api.routes.dingtalk.iot_list_devices",
                        AsyncMock(
                            return_value=[
                                {
                                    "sn": "SSYX51049784",
                                    "name": "SSYX51049784",
                                    "status": {"value": "online", "timestamp": 1_775_500_000_000},
                                    "battery": {"value": 83, "timestamp": 1_775_500_000_000},
                                    "remoteProvider": "iot",
                                    "iotAvailable": True,
                                    "dviAvailable": False,
                                }
                            ]
                        ),
                    ) as iot_list,
                ):
                    payload = await list_devices(db=db, current_user=current_user)

                dvi_list.assert_not_awaited()
                iot_list.assert_awaited_once()
                assert payload["totalCount"] == 1
                row = payload["result"][0]
                assert row["sn"] == "SSYX51049784"
                assert row["remoteProvider"] == "iot"
                assert row["status"]["value"] == "online"
                assert row["battery"]["value"] == 83

                device = (
                    await db.execute(select(Device).where(Device.device_code == "SSYX51049784"))
                ).scalar_one()
                assert device.hospital_code == "6501"
                assert device.hospital_short_name == "长沙雅美"
                assert device.status == "online"
                assert device.battery_level == 83
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_list_devices_iot_sync_status_reuses_device_query_rows() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                current_user = await _make_user(db, role="hospital_admin", hospital_code="6501")

                with (
                    patch(
                        "smart_badge_api.api.routes.dingtalk.iot_list_devices",
                        AsyncMock(
                            return_value=[
                                {
                                    "sn": "SSYX51049784",
                                    "name": "SSYX51049784",
                                    "status": {"value": "online", "timestamp": 1_775_500_000_000},
                                    "battery": {"value": 99, "timestamp": 1_775_500_000_000},
                                    "remoteProvider": "iot",
                                    "iotAvailable": True,
                                    "dviAvailable": False,
                                }
                            ]
                        ),
                    ) as iot_list,
                    patch(
                        "smart_badge_api.api.routes.dingtalk.iot_query_device_statuses",
                        AsyncMock(),
                    ) as iot_status,
                ):
                    payload = await list_devices(db=db, current_user=current_user, sync_status=True)

                iot_list.assert_awaited_once()
                iot_status.assert_not_awaited()
                assert payload["totalCount"] == 1
                row = payload["result"][0]
                assert row["status"]["value"] == "online"
                assert row["battery"]["value"] == 99

                device = (
                    await db.execute(select(Device).where(Device.device_code == "SSYX51049784"))
                ).scalar_one()
                assert device.status == "online"
                assert device.battery_level == 99
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_list_devices_filters_by_device_hospital_for_hospital_admin() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                current_user = await _make_user(
                    db,
                    username="hospital_admin",
                    role="hospital_admin",
                    hospital_code="6201",
                )
                db.add_all(
                    [
                        Device(
                            name="总院工牌",
                            device_code="SN6101",
                            hospital_code="6101",
                            hospital_short_name="米兰柏羽总院",
                            status="online",
                            is_active=True,
                        ),
                        Device(
                            name="分院工牌",
                            device_code="SN6201",
                            hospital_code="6201",
                            hospital_short_name="新机构",
                            status="online",
                            is_active=True,
                        ),
                    ]
                )
                await db.commit()

                with patch(
                    "smart_badge_api.api.routes.dingtalk.dvi_list_devices",
                    AsyncMock(
                        return_value={
                            "result": [
                                {"sn": "SN6101", "name": "总院工牌", "status": "online"},
                                {"sn": "SN6201", "name": "分院工牌", "status": "online"},
                            ]
                        }
                    ),
                ):
                    payload = await list_devices(db=db, current_user=current_user)

                assert payload["totalCount"] == 1
                assert payload["result"][0]["sn"] == "SN6201"
                assert payload["result"][0]["hospitalCode"] == "6201"
                assert payload["result"][0]["hospitalShortName"] == "新机构"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_list_devices_sync_status_refreshes_visible_device_status_and_battery() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                current_user = await _make_user(db)
                db.add(
                    Device(
                        name="一号工牌",
                        device_code="SN001",
                        status="offline",
                        battery_level=12,
                        is_active=True,
                    )
                )
                await db.commit()

                with (
                    patch(
                        "smart_badge_api.api.routes.dingtalk.dvi_list_devices",
                        AsyncMock(
                            return_value={
                                "result": [
                                    {"sn": "SN001", "name": "一号工牌", "status": "offline", "battery": 12}
                                ]
                            }
                        ),
                    ),
                    patch(
                        "smart_badge_api.api.routes.dingtalk.dvi_query_device_status",
                        AsyncMock(
                            return_value={
                                "result": [
                                    {
                                        "sn": "SN001",
                                        "status": {"value": "online", "timestamp": 1_775_500_000_000},
                                        "battery": {"value": 87, "timestamp": 1_775_500_000_000},
                                    }
                                ]
                            }
                        ),
                    ),
                ):
                    payload = await list_devices(db=db, current_user=current_user, sync_status=True)

                assert payload["totalCount"] == 1
                row = payload["result"][0]
                assert row["status"]["value"] == "online"
                assert row["battery"] == {"value": 87, "timestamp": 1_775_500_000_000}

                device = (
                    await db.execute(select(Device).where(Device.device_code == "SN001"))
                ).scalar_one()
                assert device.status == "online"
                assert device.battery_level == 87
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_device_status_uses_iot_for_changsha_yamei_devices() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                db.add_all(
                    [
                        Device(
                            name="长沙工牌",
                            device_code="SSYX51049784",
                            hospital_code="6501",
                            status="offline",
                            battery_level=10,
                            is_active=True,
                        ),
                        Device(
                            name="总院工牌",
                            device_code="SN6101",
                            hospital_code="6101",
                            status="offline",
                            battery_level=20,
                            is_active=True,
                        ),
                    ]
                )
                await db.commit()

                with (
                    patch(
                        "smart_badge_api.api.routes.dingtalk.iot_query_device_statuses",
                        AsyncMock(
                            return_value=[
                                {
                                    "sn": "SSYX51049784",
                                    "status": {"value": "online", "timestamp": 1_775_500_000_000},
                                    "battery": {"value": 82, "timestamp": 1_775_500_000_000},
                                    "remoteProvider": "iot",
                                }
                            ]
                        ),
                    ) as iot_status,
                    patch(
                        "smart_badge_api.api.routes.dingtalk.dvi_query_device_status",
                        AsyncMock(
                            return_value={
                                "result": [
                                    {
                                        "sn": "SN6101",
                                        "status": {"value": "offline", "timestamp": 1_775_500_000_000},
                                        "battery": {"value": 55, "timestamp": 1_775_500_000_000},
                                    }
                                ]
                            }
                        ),
                    ) as dvi_status,
                ):
                    payload = await get_device_status(
                        SnListRequest(snList=["SSYX51049784", "SN6101"]),
                        db=db,
                    )

                iot_status.assert_awaited_once_with(["SSYX51049784"])
                dvi_status.assert_awaited_once_with(["SN6101"])
                assert {item["sn"] for item in payload["result"]} == {"SSYX51049784", "SN6101"}

                changsha_device = (
                    await db.execute(select(Device).where(Device.device_code == "SSYX51049784"))
                ).scalar_one()
                assert changsha_device.status == "online"
                assert changsha_device.battery_level == 82
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_list_devices_returns_local_cache_when_remote_dingtalk_unavailable() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                current_user = await _make_user(db, role="hospital_admin", hospital_code="6101")
                db.add(
                    Device(
                        name="本地缓存工牌",
                        device_code="SN-CACHED",
                        hospital_code="6101",
                        hospital_short_name="米兰柏羽总院",
                        status="offline",
                        battery_level=66,
                        dingtalk_team_code="team-cached",
                        dingtalk_user_id="dt-user-cached",
                        is_active=True,
                    )
                )
                await db.commit()

                with patch(
                    "smart_badge_api.api.routes.dingtalk.dvi_list_devices",
                    AsyncMock(side_effect=DingTalkApiError("钉钉接口暂不可用")),
                ) as list_remote, patch(
                    "smart_badge_api.api.routes.dingtalk.dvi_query_device_status",
                    AsyncMock(),
                ) as query_status:
                    payload = await list_devices(db=db, current_user=current_user, sync_status=True)

                assert payload["totalCount"] == 1
                row = payload["result"][0]
                assert row["sn"] == "SN-CACHED"
                assert row["source"] == "local"
                assert row["dviAvailable"] is False
                assert row["battery"] == 66
                assert row["teamCode"] == "team-cached"
                assert row["userId"] == "dt-user-cached"
                assert list_remote.await_count == 1
                query_status.assert_not_awaited()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_bind_device_updates_local_dingtalk_binding_cache() -> None:
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
                await db.commit()
                await db.refresh(staff)

                with patch(
                    "smart_badge_api.api.routes.dingtalk.dvi_update_device_binding",
                    AsyncMock(return_value={"success": True}),
                ):
                    payload = await bind_device(
                        BindDeviceRequest(
                            sn="SN002",
                            teamCode="team-001",
                            userId="dt_user_002",
                        ),
                        db=db,
                    )

                assert payload["success"] is True
                device = (await db.execute(select(Device).where(Device.device_code == "SN002"))).scalar_one()
                assert device.dingtalk_team_code == "team-001"
                assert device.dingtalk_user_id == "dt_user_002"
                assert device.dingtalk_binding_synced_at is not None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_bind_system_device_persists_local_staff_binding() -> None:
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
                await db.commit()
                await db.refresh(staff)
                current_user = await _make_user(db)

                payload = await bind_system_device(
                    SystemBindDeviceRequest(
                        sn="SN003",
                        staffId=staff.id,
                        deviceName="三号工牌",
                    ),
                    db=db,
                    current_user=current_user,
                )

                assert payload["success"] is True
                assert payload["staffId"] == staff.id
                device = (
                    await db.execute(select(Device).where(Device.device_code == "SN003"))
                ).scalar_one()
                assert device.staff_id == staff.id
                assert device.name == "三号工牌"
                await db.refresh(staff)
                assert staff.badge_id == "SN003"
                bindings = (
                    await db.execute(select(DeviceStaffBinding).where(DeviceStaffBinding.device_id == device.id))
                ).scalars().all()
                assert len(bindings) == 1
                assert bindings[0].staff_id == staff.id
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_bind_system_device_requires_overlap_confirmation_and_splits_existing_range() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff_a = Staff(
                    name="员工A",
                    external_account="81010001",
                    permission_role="staff",
                    is_active=True,
                )
                staff_b = Staff(
                    name="员工B",
                    external_account="81010002",
                    permission_role="staff",
                    is_active=True,
                )
                db.add_all([staff_a, staff_b])
                await db.commit()
                await db.refresh(staff_a)
                await db.refresh(staff_b)
                current_user = await _make_user(db)

                await bind_system_device(
                    SystemBindDeviceRequest(
                        sn="SN003X",
                        staffId=staff_a.id,
                        deviceName="测试工牌",
                    ),
                    db=db,
                    current_user=current_user,
                )

                try:
                    await bind_system_device(
                        SystemBindDeviceRequest(
                            sn="SN003X",
                            staffId=staff_b.id,
                            deviceName="测试工牌",
                            effectiveStart="2026-05-01T00:00:00+00:00",
                            effectiveEnd="2026-06-01T00:00:00+00:00",
                        ),
                        db=db,
                        current_user=current_user,
                    )
                    assert False, "expected overlap confirmation"
                except HTTPException as exc:
                    assert exc.status_code == 409
                    assert isinstance(exc.detail, dict)
                    assert exc.detail["code"] == "device_binding_overlap"

                payload = await bind_system_device(
                    SystemBindDeviceRequest(
                        sn="SN003X",
                        staffId=staff_b.id,
                        deviceName="测试工牌",
                        effectiveStart="2026-05-01T00:00:00+00:00",
                        effectiveEnd="2026-06-01T00:00:00+00:00",
                        overrideOverlap=True,
                    ),
                    db=db,
                    current_user=current_user,
                )

                assert payload["success"] is True
                device = (
                    await db.execute(select(Device).where(Device.device_code == "SN003X"))
                ).scalar_one()
                bindings = (
                    await db.execute(
                        select(DeviceStaffBinding)
                        .where(DeviceStaffBinding.device_id == device.id)
                        .order_by(DeviceStaffBinding.effective_from.asc())
                    )
                ).scalars().all()

                assert len(bindings) == 3
                assert bindings[0].staff_id == staff_a.id
                assert bindings[0].effective_from.replace(tzinfo=timezone.utc) == EARLIEST_BINDING_AT
                assert bindings[0].effective_to.replace(tzinfo=timezone.utc).isoformat() == "2026-05-01T00:00:00+00:00"
                assert bindings[1].staff_id == staff_b.id
                assert bindings[1].effective_from.replace(tzinfo=timezone.utc).isoformat() == "2026-05-01T00:00:00+00:00"
                assert bindings[1].effective_to.replace(tzinfo=timezone.utc).isoformat() == "2026-06-01T00:00:00+00:00"
                assert bindings[2].staff_id == staff_a.id
                assert bindings[2].effective_from.replace(tzinfo=timezone.utc).isoformat() == "2026-06-01T00:00:00+00:00"
                assert bindings[2].effective_to is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_unbind_device_clears_local_dingtalk_binding_cache() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="杜娟",
                    external_account="81019369",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.flush()
                db.add(
                    Device(
                        name="四号工牌",
                        device_code="SN004",
                        staff_id=staff.id,
                        status="online",
                        dingtalk_team_code="team-001",
                        dingtalk_user_id="dt_user_004",
                        is_active=True,
                    )
                )
                await db.commit()

                with patch(
                    "smart_badge_api.api.routes.dingtalk.dvi_update_device_binding",
                    AsyncMock(return_value={"success": True}),
                ):
                    payload = await unbind_device(
                        BindDeviceRequest(
                            sn="SN004",
                            teamCode="team-001",
                            userId="dt_user_004",
                        ),
                        db=db,
                    )

                assert payload["success"] is True
                device = (
                    await db.execute(select(Device).where(Device.device_code == "SN004"))
                ).scalar_one()
                assert device.staff_id == staff.id
                assert device.dingtalk_team_code is None
                assert device.dingtalk_user_id is None
                assert device.dingtalk_binding_synced_at is not None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_unbind_system_device_clears_local_system_binding() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="顾问A",
                    external_account="81010001",
                    badge_id="SN005",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.flush()
                db.add(
                    Device(
                        name="五号工牌",
                        device_code="SN005",
                        staff_id=staff.id,
                        status="online",
                        is_active=True,
                    )
                )
                await db.flush()
                device = (
                    await db.execute(select(Device).where(Device.device_code == "SN005"))
                ).scalar_one()
                db.add(
                    DeviceStaffBinding(
                        device_id=device.id,
                        staff_id=staff.id,
                        effective_from=EARLIEST_BINDING_AT,
                    )
                )
                db.add(
                    Recording(
                        file_name="0501_100000.mp3",
                        file_path="uploads/0501_100000.mp3",
                        device_id="SN005",
                        staff_id=staff.id,
                    )
                )
                await db.commit()
                current_user = await _make_user(db)

                try:
                    await unbind_system_device(
                        SystemUnbindDeviceRequest(sn="SN005"),
                        db=db,
                        current_user=current_user,
                    )
                    assert False, "expected confirmation requirement"
                except HTTPException as exc:
                    assert exc.status_code == 400

                payload = await unbind_system_device(
                    SystemUnbindDeviceRequest(sn="SN005", clearHistory=True, clearRecordingOwners=True),
                    db=db,
                    current_user=current_user,
                )

                assert payload["success"] is True
                device = (
                    await db.execute(select(Device).where(Device.device_code == "SN005"))
                ).scalar_one()
                assert device.staff_id is None
                await db.refresh(staff)
                assert staff.badge_id is None
                bindings = (
                    await db.execute(select(DeviceStaffBinding).where(DeviceStaffBinding.device_id == device.id))
                ).scalars().all()
                assert bindings == []
                recording = (await db.execute(select(Recording).where(Recording.file_name == "0501_100000.mp3"))).scalar_one()
                assert recording.staff_id is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_attach_archive_recording_bindings_matches_display_file_name_when_staged_name_is_old() -> None:
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
                customer = Customer(name="玲")
                db.add_all([staff, customer])
                await db.flush()
                visit = Visit(
                    customer_id=customer.id,
                    consultant_id=staff.id,
                    status="consulted",
                )
                db.add(visit)
                await db.flush()
                recording = Recording(
                    file_name="0323_180519.mp3",
                    file_path="dingtalk_staging/archive/SSYX41022508/202603/0323_180519.mp3",
                    staff_id=staff.id,
                    visit_id=visit.id,
                    status="analyzed",
                )
                db.add(recording)
                await db.flush()
                db.add(
                    RecordingVisitLink(
                        recording_id=recording.id,
                        visit_id=visit.id,
                        is_primary=True,
                        source="manual",
                    )
                )
                await db.commit()

                [item] = await _attach_archive_recording_bindings(
                    db,
                    [
                        {
                            "staged_file_name": "23_180519.mp3",
                            "display_file_name": "0323_180519.mp3",
                            "archive_file_name": "0323_180519.mp3",
                            "has_transcript": True,
                            "pipeline_status": "analyzed",
                        }
                    ],
                )

                assert item["recording_id"] == recording.id
                assert item["visit_id"] == visit.id
                assert item["has_visit_link"] is True
                assert item["needs_visit_link"] is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_build_archive_recording_summary_resolves_legacy_manifest_paths(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))

    from smart_badge_api.core.config import get_settings

    get_settings.cache_clear()
    try:
        transcript_path = tmp_path / "uploads" / "dingtalk_staging" / "transcripts" / "SN001__file-001.transcript.json"
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text("{}", encoding="utf-8")

        analysis_path = tmp_path / "uploads" / "dingtalk_staging" / "results" / "SN001__file-001.result.json"
        analysis_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_path.write_text("{}", encoding="utf-8")

        summary = _build_archive_recording_summary(
            {
                "fileId": "file-001",
                "sn": "SN001",
                "audioPath": str(tmp_path / "uploads" / "archive.mp3"),
            },
            {
                "deviceCode": "SN001",
                "status": "analyzed",
                "transcriptPath": "/app/uploads/dingtalk_staging/transcripts/SN001__file-001.transcript.json",
                "analysisResultPath": "/app/uploads/dingtalk_staging/results/SN001__file-001.result.json",
            },
        )

        assert summary is not None
        assert summary["has_transcript"] is True
        assert summary["has_analysis"] is True
    finally:
        get_settings.cache_clear()
