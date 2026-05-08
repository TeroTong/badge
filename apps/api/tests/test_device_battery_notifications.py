import asyncio
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.dingtalk import list_devices
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import Device, DeviceBatteryReminder, Staff, User, WecomTenant
from smart_badge_api.device_battery_notifications import handle_device_battery_update


async def _make_user(db, *, role: str = "system_admin", hospital_code: str | None = None) -> User:
    user = User(
        username=f"{role}_user",
        hashed_password="hashed",
        display_name="管理员",
        role=role,
        hospital_code=hospital_code,
        hospital_name="测试机构" if hospital_code else None,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _seed_bound_badge(db) -> tuple[Staff, Device]:
    staff = Staff(
        name="张三",
        external_account="90010001",
        wecom_user_id="zhangsan",
        wecom_corp_id="ww6101",
        hospital_code="6101",
        hospital_short_name="米兰柏羽总院",
        permission_role="staff",
        is_active=True,
    )
    db.add(staff)
    await db.flush()
    device = Device(
        name="测试工牌",
        device_code="SSYX41020001",
        staff_id=staff.id,
        hospital_code="6101",
        hospital_short_name="米兰柏羽总院",
        battery_level=28,
        status="online",
        is_active=True,
    )
    db.add(device)
    db.add(
        WecomTenant(
            name="米兰柏羽总院",
            default_hospital_code="6101",
            default_hospital_name="米兰柏羽总院",
            corp_id="ww6101",
            agent_id="1000002",
            agent_secret="secret",
            frontend_url="https://gongpai.example.com",
            is_active=True,
            is_default=True,
        )
    )
    await db.commit()
    await db.refresh(staff)
    await db.refresh(device)
    return staff, device


def test_low_battery_notification_sends_once_per_day() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                _staff, device = await _seed_bound_badge(db)
                sender = AsyncMock(return_value={"errcode": 0})
                with patch("smart_badge_api.device_battery_notifications._send_low_battery_message_via_platform", sender):
                    first_sent = await handle_device_battery_update(db, device, battery_level=29)
                    second_sent = await handle_device_battery_update(db, device, battery_level=28)

                assert first_sent is True
                assert second_sent is False
                assert sender.await_count == 1
                reminder = (
                    await db.execute(select(DeviceBatteryReminder).where(DeviceBatteryReminder.device_code == device.device_code))
                ).scalar_one()
                assert reminder.last_battery_level == 28
                assert reminder.alert_active is True
                assert reminder.last_notified_date is not None
                assert reminder.last_error is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_recovered_battery_resets_active_alert() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                _staff, device = await _seed_bound_badge(db)
                sender = AsyncMock(return_value={"errcode": 0})
                with patch("smart_badge_api.device_battery_notifications._send_low_battery_message_via_platform", sender):
                    await handle_device_battery_update(db, device, battery_level=29)
                    recovered_sent = await handle_device_battery_update(db, device, battery_level=30)

                assert recovered_sent is False
                reminder = (
                    await db.execute(select(DeviceBatteryReminder).where(DeviceBatteryReminder.device_code == device.device_code))
                ).scalar_one()
                assert reminder.alert_active is False
                assert reminder.recovered_at is not None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_list_devices_triggers_low_battery_notification() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                await _seed_bound_badge(db)
                current_user = await _make_user(db)
                sender = AsyncMock(return_value={"errcode": 0})
                with (
                    patch(
                        "smart_badge_api.api.routes.dingtalk.dvi_list_devices",
                        AsyncMock(
                            return_value={
                                "result": [
                                    {
                                        "sn": "SSYX41020001",
                                        "name": "测试工牌",
                                        "status": "online",
                                        "battery": 29,
                                    }
                                ]
                            }
                        ),
                    ),
                    patch("smart_badge_api.device_battery_notifications._send_low_battery_message_via_platform", sender),
                ):
                    payload = await list_devices(db=db, current_user=current_user)

                assert payload["totalCount"] == 1
                assert payload["result"][0]["battery"] == 29
                assert sender.await_count == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_battery_at_threshold_does_not_send_notification() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                _staff, device = await _seed_bound_badge(db)
                sender = AsyncMock(return_value={"errcode": 0})
                with patch("smart_badge_api.device_battery_notifications._send_low_battery_message_via_platform", sender):
                    sent = await handle_device_battery_update(db, device, battery_level=30)

                assert sent is False
                assert sender.await_count == 0
                reminder = (
                    await db.execute(select(DeviceBatteryReminder).where(DeviceBatteryReminder.device_code == device.device_code))
                ).scalar_one()
                assert reminder.last_battery_level == 30
                assert reminder.alert_active is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())
