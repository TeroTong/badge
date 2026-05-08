from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import Device, DeviceBatteryReminder, Staff
from smart_badge_api.message_push import (
    MessagePushApiError,
    MessagePushConfigError,
    resolve_message_push_auth_code,
    send_message_push,
)


LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def _clean_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _local_date_key(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(LOCAL_TZ).date().isoformat()


async def _load_or_create_reminder(db: AsyncSession, device: Device, staff_id: str | None) -> DeviceBatteryReminder:
    reminder = (
        await db.execute(
            select(DeviceBatteryReminder)
            .where(
                DeviceBatteryReminder.device_code == device.device_code,
                (
                    DeviceBatteryReminder.staff_id == staff_id
                    if staff_id
                    else DeviceBatteryReminder.staff_id.is_(None)
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if reminder is not None:
        return reminder

    reminder = DeviceBatteryReminder(
        device_id=device.id,
        device_code=device.device_code,
        staff_id=staff_id,
        alert_active=False,
    )
    db.add(reminder)
    await db.flush()
    return reminder


def _build_low_battery_message(*, device: Device, staff: Staff, battery_level: int) -> str:
    staff_name = _clean_text(staff.name) or "您好"
    return (
        f"{staff_name}，您的工牌 {device.device_code} 当前电量为 {battery_level}%，"
        "请及时充电，避免影响面诊录音上传和接诊记录。"
    )


async def _send_low_battery_message_via_platform(
    *,
    device: Device,
    staff: Staff,
    battery_level: int,
) -> dict:
    hospital_code = _clean_text(staff.hospital_code) or _clean_text(device.hospital_code)
    auth_code = resolve_message_push_auth_code(hospital_code)
    if not auth_code:
        raise MessagePushConfigError(f"未找到机构 {hospital_code or '-'} 的消息平台 Auth Code")

    employee_code = _clean_text(staff.external_account)
    if not employee_code:
        raise MessagePushConfigError("员工未配置员工编号，无法通过消息平台发送低电量提醒")

    settings = get_settings()
    return await send_message_push(
        title="工牌低电量提醒",
        content=_build_low_battery_message(device=device, staff=staff, battery_level=battery_level),
        auth_code=auth_code,
        targets=[employee_code],
        biz_user_id=settings.message_push_low_battery_biz_user_id,
        org_code=hospital_code,
        msg_type="text",
    )


async def handle_device_battery_update(
    db: AsyncSession,
    device: Device,
    *,
    battery_level: int | None = None,
) -> bool:
    settings = get_settings()
    if not settings.device_low_battery_alert_enabled:
        return False

    current_battery = battery_level if battery_level is not None else device.battery_level
    if current_battery is None:
        return False

    staff_id = _clean_text(device.staff_id)
    reminder = await _load_or_create_reminder(db, device, staff_id)
    reminder.device_id = device.id
    reminder.device_code = device.device_code
    reminder.staff_id = staff_id
    reminder.last_battery_level = current_battery

    now = _now_utc()
    if current_battery >= settings.device_low_battery_recovery_threshold:
        if reminder.alert_active:
            reminder.alert_active = False
            reminder.recovered_at = now
            reminder.last_error = None
            await db.commit()
        return False

    if current_battery >= settings.device_low_battery_threshold:
        await db.commit()
        return False

    today_key = _local_date_key(now)
    if reminder.last_notified_date == today_key:
        reminder.alert_active = True
        await db.commit()
        return False

    if not staff_id:
        reminder.alert_active = True
        reminder.last_error = "工牌未绑定员工，无法发送企业微信低电量提醒"
        await db.commit()
        return False

    staff = await db.get(Staff, staff_id)
    if staff is None or not staff.is_active:
        reminder.alert_active = True
        reminder.last_error = "绑定员工不存在或已停用，无法发送企业微信低电量提醒"
        await db.commit()
        return False

    wecom_user_id = _clean_text(staff.wecom_user_id)
    reminder.wecom_user_id = wecom_user_id
    reminder.wecom_corp_id = _clean_text(staff.wecom_corp_id)

    try:
        await _send_low_battery_message_via_platform(device=device, staff=staff, battery_level=current_battery)
    except (MessagePushApiError, MessagePushConfigError) as exc:
        reminder.alert_active = True
        reminder.last_error = str(exc)
        await db.commit()
        return False

    reminder.alert_active = True
    reminder.last_notified_at = now
    reminder.last_notified_date = today_key
    reminder.last_error = None
    await db.commit()
    return True
