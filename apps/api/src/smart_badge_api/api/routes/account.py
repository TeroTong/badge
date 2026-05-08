from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.audit import append_audit_log
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.account_provisioning import resolve_staff_for_user
from smart_badge_api.core.permissions import normalize_permission_role, permission_role_level
from smart_badge_api.core.security import hash_password, verify_password
from smart_badge_api.device_battery_notifications import handle_device_battery_update
from smart_badge_api.db.models import AuditLog, Device, PositionProfile, Staff, StaffManagementRelation, User
from smart_badge_api.db.session import get_db
from smart_badge_api.dingtalk import (
    DingTalkApiError,
    DingTalkConfigError,
    dvi_control_recording,
    dvi_list_devices,
    dvi_query_device_status,
)
from smart_badge_api.dingtalk_iot import (
    iot_control_recording,
    iot_list_devices,
    iot_query_device_statuses,
    is_iot_hospital_code,
)
from smart_badge_api.schemas.audit_logs import AuditLogOut
from smart_badge_api.schemas.profile import (
    AccountProfileOut,
    AccountProfileUpdate,
    ChangePasswordRequest,
    MessageOut,
    MyBadgeOut,
)

router = APIRouter(prefix="/account", tags=["个人中心"])


_MANAGED_REMOTE_CACHE_TTL_SECONDS = 6.0
_MANAGED_REMOTE_STALE_TTL_SECONDS = 60.0
_managed_remote_cache_lock: asyncio.Lock | None = None
_managed_remote_device_cache: dict[tuple[str, str], tuple[float, dict[str, object]]] = {}


def _get_managed_remote_cache_lock() -> asyncio.Lock:
    global _managed_remote_cache_lock
    if _managed_remote_cache_lock is None:
        _managed_remote_cache_lock = asyncio.Lock()
    return _managed_remote_cache_lock


def _cache_key(provider: str, code: str) -> tuple[str, str]:
    return provider, code


def _get_cached_remote_device(
    provider: str,
    code: str,
    *,
    now: float,
    allow_stale: bool = False,
) -> dict[str, object] | None:
    cached = _managed_remote_device_cache.get(_cache_key(provider, code))
    if cached is None:
        return None
    cached_at, payload = cached
    ttl = _MANAGED_REMOTE_STALE_TTL_SECONDS if allow_stale else _MANAGED_REMOTE_CACHE_TTL_SECONDS
    if now - cached_at <= ttl:
        return dict(payload)
    return None


def _store_cached_remote_devices(provider: str, rows: list[dict[str, object]], *, now: float) -> None:
    for row in rows:
        sn = _clean_text(row.get("sn"))
        if sn:
            _managed_remote_device_cache[_cache_key(provider, sn)] = (now, dict(row))


@dataclass
class _MyBadgeContext:
    staff: Staff | None
    device: Device | None
    position_name: str | None = None
    remote_device: dict[str, object] | None = None
    online: bool | None = None
    battery_level: int | None = None
    recording_active: bool = False
    recording_started_at: str | None = None
    remote_warning: str | None = None
    remote_provider: str | None = None

    @property
    def team_code(self) -> str | None:
        if not self.remote_device:
            return None
        return _clean_text(self.remote_device.get("teamCode"))

    @property
    def user_id(self) -> str | None:
        if not self.remote_device:
            return None
        return _clean_text(self.remote_device.get("userId"))

    @property
    def can_control_recording(self) -> bool:
        return self.remote_provider == "iot" or bool(self.team_code and self.user_id)

    @property
    def is_recording(self) -> bool:
        return self.recording_active or self.recording_started_at is not None


def _activity_name_candidates(user: User) -> list[str]:
    candidates: list[str] = []
    for value in (user.display_name, user.username):
        normalized = value.strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates


def _handle_dingtalk_error(exc: DingTalkConfigError | DingTalkApiError) -> HTTPException:
    if isinstance(exc, DingTalkConfigError):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    return HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


def _clean_text(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_nested_primitive(value: object) -> str | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, dict) and "value" in value:
        nested = value["value"]
        if isinstance(nested, bool):
            return None
        if isinstance(nested, (str, int)):
            return nested
        if isinstance(nested, float):
            return int(nested)
    return None


def _coerce_int(value: object) -> int | None:
    primitive = _read_nested_primitive(value)
    if isinstance(primitive, int):
        return primitive
    if isinstance(primitive, str) and primitive.isdigit():
        return int(primitive)
    return None


def _coerce_status_text(value: object) -> str | None:
    primitive = _read_nested_primitive(value)
    if not isinstance(primitive, str):
        return None
    normalized = primitive.strip().lower()
    return normalized or None


def _coerce_online(value: object) -> bool | None:
    normalized = _coerce_status_text(value)
    if normalized is None:
        return None
    if normalized == "offline":
        return False
    if normalized in {"online", "idle", "recording"}:
        return True
    return None


def _coerce_recording_active(*, status_value: object, recording_start_value: object) -> bool:
    if _coerce_timestamp_iso(recording_start_value) is not None:
        return True
    return _coerce_status_text(status_value) == "recording"


def _coerce_timestamp_iso(value: object) -> str | None:
    timestamp_ms = _coerce_int(value)
    if timestamp_ms is None or timestamp_ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _device_uses_iot(staff: Staff | None, device: Device | None) -> bool:
    return is_iot_hospital_code(device.hospital_code if device and device.hospital_code else staff.hospital_code if staff else None)


def _to_activity_out(item: AuditLog) -> AuditLogOut:
    return AuditLogOut(
        id=item.id,
        operator_name=item.operator_name,
        ip_address=item.ip_address,
        module_name=item.module_name,
        action_name=item.action_name,
        content=item.content,
        created_at=item.created_at.isoformat() if item.created_at else "",
    )


async def _load_recent_activities(db: AsyncSession, user: User, *, limit: int = 8) -> tuple[list[AuditLog], int]:
    candidates = _activity_name_candidates(user)
    if not candidates:
        return [], 0

    query = select(AuditLog).where(AuditLog.operator_name.in_(candidates)).order_by(AuditLog.created_at.desc())
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (await db.execute(query.limit(limit))).scalars().all()
    return rows, total


async def _build_account_profile_out(db: AsyncSession, user: User) -> AccountProfileOut:
    recent_activities, activity_count = await _load_recent_activities(db, user)
    last_activity_at = recent_activities[0].created_at.isoformat() if recent_activities else None
    return AccountProfileOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=normalize_permission_role(user.role),
        is_active=user.is_active,
        created_at=user.created_at.isoformat() if user.created_at else "",
        updated_at=user.updated_at.isoformat() if user.updated_at else "",
        activity_count=activity_count,
        last_activity_at=last_activity_at,
        recent_activities=[_to_activity_out(item) for item in recent_activities],
    )


async def _load_bound_badge_context(db: AsyncSession, user: User) -> tuple[_MyBadgeContext | None, str | None]:
    staff = await resolve_staff_for_user(db, user=user, persist_link=True)
    if staff is None:
        return None, "当前账号未关联系统人员"

    return await _load_badge_context_for_staff(db, staff)


async def _load_badge_context_for_staff(db: AsyncSession, staff: Staff) -> tuple[_MyBadgeContext | None, str | None]:
    position_name = None
    if staff.position_id:
        position = await db.get(PositionProfile, staff.position_id)
        position_name = position.name if position else None

    device = (
        await db.execute(
            select(Device)
            .where(Device.staff_id == staff.id)
            .order_by(Device.updated_at.desc(), Device.created_at.desc(), Device.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if device is None:
        return (
            _MyBadgeContext(
                staff=staff,
                device=None,
                position_name=position_name,
            ),
            "当前员工暂未绑定工牌",
        )

    remote_warning: str | None = None
    remote_device: dict[str, object] | None = None
    online: bool | None = None
    battery_level: int | None = None
    recording_active = False
    recording_started_at: str | None = None
    status_row_seen = False
    uses_iot = _device_uses_iot(staff, device)
    remote_provider = "iot" if uses_iot else "dvi"

    try:
        if uses_iot:
            candidates = await iot_list_devices(device_no=device.device_code)
        else:
            payload = await dvi_list_devices(max_results=20, sn=device.device_code)
            candidates = payload.get("result") or []
        remote_device = next(
            (item for item in candidates if _clean_text(item.get("sn")) == device.device_code),
            candidates[0] if candidates else None,
        )
    except (DingTalkConfigError, DingTalkApiError) as exc:
        remote_warning = str(exc)

    try:
        if uses_iot:
            status_rows = await iot_query_device_statuses([device.device_code])
        else:
            payload = await dvi_query_device_status([device.device_code])
            status_rows = payload.get("result") or []
        status_row = next(
            (item for item in status_rows if _clean_text(item.get("sn")) == device.device_code),
            status_rows[0] if status_rows else None,
        )
        if status_row:
            status_row_seen = True
            online = _coerce_online(status_row.get("status"))
            battery_level = _coerce_int(status_row.get("battery"))
            recording_active = _coerce_recording_active(
                status_value=status_row.get("status"),
                recording_start_value=status_row.get("recordingStartTime"),
            )
            recording_started_at = _coerce_timestamp_iso(status_row.get("recordingStartTime"))
    except (DingTalkConfigError, DingTalkApiError) as exc:
        if remote_warning is None:
            remote_warning = str(exc)

    if online is None and remote_device is not None:
        online = _coerce_online(remote_device.get("status"))
    if battery_level is None and remote_device is not None:
        battery_level = _coerce_int(remote_device.get("battery"))
    if not status_row_seen and not recording_active and remote_device is not None:
        recording_active = _coerce_recording_active(
            status_value=remote_device.get("status"),
            recording_start_value=remote_device.get("recordingStartTime"),
        )
    if not status_row_seen and recording_started_at is None and remote_device is not None:
        recording_started_at = _coerce_timestamp_iso(remote_device.get("recordingStartTime"))

    changed = False
    remote_name = _clean_text(remote_device.get("name")) if remote_device else None
    if remote_name and device.name != remote_name:
        device.name = remote_name
        changed = True
    if uses_iot:
        staff_hospital_code = _clean_text(staff.hospital_code)
        staff_hospital_short_name = _clean_text(staff.hospital_short_name)
        if staff_hospital_code and device.hospital_code != staff_hospital_code:
            device.hospital_code = staff_hospital_code
            changed = True
        if staff_hospital_short_name and device.hospital_short_name != staff_hospital_short_name:
            device.hospital_short_name = staff_hospital_short_name
            changed = True
    derived_status = "online" if online is True else "offline" if online is False else _clean_text(remote_device.get("status")) if remote_device else None
    if derived_status and device.status != derived_status:
        device.status = derived_status
        changed = True
    if battery_level is not None and device.battery_level != battery_level:
        device.battery_level = battery_level
        changed = True
    if changed:
        await db.commit()
        await db.refresh(device)
    if battery_level is not None:
        await handle_device_battery_update(db, device, battery_level=battery_level)

    return (
        _MyBadgeContext(
            staff=staff,
            device=device,
            position_name=position_name,
            remote_device=remote_device,
            online=online,
            battery_level=battery_level,
            recording_active=recording_active,
            recording_started_at=recording_started_at,
            remote_warning=remote_warning,
            remote_provider=remote_provider,
        ),
        None,
    )


def _build_my_badge_out(context: _MyBadgeContext | None, *, reason: str | None = None) -> MyBadgeOut:
    if context is None or context.staff is None:
        return MyBadgeOut(bound=False, reason=reason or "当前账号暂未绑定工牌")

    staff = context.staff
    if context.device is None:
        return MyBadgeOut(
            bound=False,
            reason=reason or "当前员工暂未绑定工牌",
            staff_id=staff.id,
            staff_name=staff.name,
            external_account=staff.external_account,
            hospital_short_name=staff.hospital_short_name,
            position_name=context.position_name,
            can_control_recording=False,
            is_recording=False,
        )

    device = context.device
    remote_device = context.remote_device or {}
    device_name = _clean_text(remote_device.get("name")) or device.name
    status = (
        "online"
        if context.online is True
        else "offline"
        if context.online is False
        else (_clean_text(remote_device.get("status")) or device.status)
    )

    return MyBadgeOut(
        bound=True,
        device_id=device.id,
        device_code=device.device_code,
        device_name=device_name,
        staff_id=staff.id,
        staff_name=staff.name,
        external_account=staff.external_account,
        hospital_short_name=staff.hospital_short_name,
        position_name=context.position_name,
        status=status,
        online=context.online,
        battery_level=context.battery_level if context.battery_level is not None else device.battery_level,
        team_code=context.team_code,
        user_id=context.user_id,
        can_control_recording=context.can_control_recording,
        is_recording=context.is_recording,
        recording_started_at=context.recording_started_at,
        remote_warning=context.remote_warning,
    )


def _managed_badge_target_visible(user: User, manager: Staff, target: Staff) -> bool:
    if target.id == manager.id:
        return True
    if normalize_permission_role(user.role) == "super_admin":
        return True
    return permission_role_level(target.permission_role) <= permission_role_level(user.role)


async def _load_latest_devices_by_staff(db: AsyncSession, staff_ids: list[str]) -> dict[str, Device]:
    if not staff_ids:
        return {}
    ranked_devices = (
        select(
            Device.id.label("device_id"),
            func.row_number()
            .over(
                partition_by=Device.staff_id,
                order_by=(Device.updated_at.desc(), Device.created_at.desc(), Device.id.desc()),
            )
            .label("rank"),
        )
        .where(Device.staff_id.in_(staff_ids))
        .subquery()
    )
    rows = (
        await db.execute(
            select(Device)
            .join(ranked_devices, ranked_devices.c.device_id == Device.id)
            .where(ranked_devices.c.rank == 1)
        )
    ).scalars().all()
    return {device.staff_id: device for device in rows if device.staff_id}


async def _load_position_names(db: AsyncSession, staff_items: list[Staff]) -> dict[str, str]:
    position_ids = sorted({staff.position_id for staff in staff_items if staff.position_id})
    if not position_ids:
        return {}
    rows = (
        await db.execute(
            select(PositionProfile.id, PositionProfile.name).where(PositionProfile.id.in_(position_ids))
        )
    ).all()
    return {position_id: name for position_id, name in rows if position_id and name}


async def _load_managed_remote_devices(device_items: list[Device]) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
    if not device_items:
        return {}, {}

    remote_by_code: dict[str, dict[str, object]] = {}
    warning_by_code: dict[str, str] = {}
    iot_codes: set[str] = set()
    dvi_codes: set[str] = set()
    for device in device_items:
        device_code = _clean_text(device.device_code)
        if not device_code:
            continue
        if is_iot_hospital_code(device.hospital_code):
            iot_codes.add(device_code)
        else:
            dvi_codes.add(device_code)

    async with _get_managed_remote_cache_lock():
        now = time.monotonic()
        missing_iot_codes: set[str] = set()
        missing_dvi_codes: set[str] = set()

        for code in iot_codes:
            cached = _get_cached_remote_device("iot", code, now=now)
            if cached is None:
                missing_iot_codes.add(code)
            else:
                remote_by_code[code] = cached
        for code in dvi_codes:
            cached = _get_cached_remote_device("dvi", code, now=now)
            if cached is None:
                missing_dvi_codes.add(code)
            else:
                remote_by_code[code] = cached

        if missing_iot_codes:
            try:
                if len(missing_iot_codes) >= 8:
                    rows = await iot_list_devices(max_pages=20)
                else:
                    rows = await iot_query_device_statuses(sorted(missing_iot_codes))
                normalized_rows = [row for row in rows if isinstance(row, dict)]
                _store_cached_remote_devices("iot", normalized_rows, now=time.monotonic())
                for row in normalized_rows:
                    sn = _clean_text(row.get("sn"))
                    if sn and sn in iot_codes:
                        remote_by_code[sn] = dict(row)
            except (DingTalkConfigError, DingTalkApiError) as exc:
                warning = str(exc)
                for code in missing_iot_codes:
                    cached = _get_cached_remote_device("iot", code, now=now, allow_stale=True)
                    if cached is not None:
                        remote_by_code[code] = cached
                    warning_by_code[code] = warning

        if missing_dvi_codes:
            try:
                payload = await dvi_query_device_status(sorted(missing_dvi_codes))
                rows = [row for row in (payload.get("result") or []) if isinstance(row, dict)]
                _store_cached_remote_devices("dvi", rows, now=time.monotonic())
                for row in rows:
                    sn = _clean_text(row.get("sn"))
                    if sn and sn in dvi_codes:
                        remote_by_code[sn] = dict(row)
            except (DingTalkConfigError, DingTalkApiError) as exc:
                warning = str(exc)
                for code in missing_dvi_codes:
                    cached = _get_cached_remote_device("dvi", code, now=now, allow_stale=True)
                    if cached is not None:
                        remote_by_code[code] = cached
                    warning_by_code[code] = warning

    return remote_by_code, warning_by_code


def _build_managed_badge_context(
    *,
    staff: Staff,
    device: Device | None,
    position_name: str | None,
    remote_device: dict[str, object] | None,
    remote_warning: str | None,
) -> _MyBadgeContext:
    if device is None:
        return _MyBadgeContext(staff=staff, device=None, position_name=position_name)

    uses_iot = _device_uses_iot(staff, device)
    online = None
    battery_level = None
    recording_active = False
    recording_started_at = None
    if remote_device is not None:
        online = _coerce_online(remote_device.get("status"))
        battery_level = _coerce_int(remote_device.get("battery"))
        recording_active = _coerce_recording_active(
            status_value=remote_device.get("status"),
            recording_start_value=remote_device.get("recordingStartTime"),
        )
        recording_started_at = _coerce_timestamp_iso(remote_device.get("recordingStartTime"))

    return _MyBadgeContext(
        staff=staff,
        device=device,
        position_name=position_name,
        remote_device=remote_device,
        online=online,
        battery_level=battery_level,
        recording_active=recording_active,
        recording_started_at=recording_started_at,
        remote_warning=remote_warning,
        remote_provider="iot" if uses_iot else "dvi",
    )


async def _refresh_managed_device_cache(db: AsyncSession, contexts: list[_MyBadgeContext]) -> None:
    changed = False
    for context in contexts:
        if context.device is None or context.remote_device is None:
            continue
        device = context.device
        remote_device = context.remote_device
        remote_name = _clean_text(remote_device.get("name"))
        if remote_name and device.name != remote_name:
            device.name = remote_name
            changed = True
        derived_status = (
            "online"
            if context.online is True
            else "offline"
            if context.online is False
            else _clean_text(remote_device.get("status"))
        )
        if derived_status and device.status != derived_status:
            device.status = derived_status
            changed = True
        if context.battery_level is not None and device.battery_level != context.battery_level:
            device.battery_level = context.battery_level
            changed = True
    if changed:
        await db.commit()


async def _require_my_badge_recording_context(db: AsyncSession, user: User) -> _MyBadgeContext:
    context, reason = await _load_bound_badge_context(db, user)
    if context is None or context.staff is None or context.device is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, reason or "当前账号暂未绑定工牌")
    if not context.can_control_recording:
        if context.remote_warning:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"当前工牌远端状态不可用：{context.remote_warning}")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "当前工牌尚未完成钉钉侧绑定，暂不能控制录音")
    return context


@router.get("/me", response_model=AccountProfileOut)
async def get_account_profile(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await _build_account_profile_out(db, user)


@router.get("/my-badge", response_model=MyBadgeOut)
async def get_my_badge(
    response: Response = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    context, reason = await _load_bound_badge_context(db, user)
    return _build_my_badge_out(context, reason=reason)


@router.get("/managed-badges", response_model=list[MyBadgeOut])
async def get_managed_badges(
    response: Response = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    staff = await resolve_staff_for_user(db, user=user, persist_link=True)
    if staff is None:
        return []

    managed_staff = (
        await db.execute(
            select(Staff)
            .join(StaffManagementRelation, StaffManagementRelation.subordinate_staff_id == Staff.id)
            .where(
                StaffManagementRelation.manager_staff_id == staff.id,
                Staff.is_active.is_(True),
            )
            .order_by(Staff.hospital_code.asc(), Staff.name.asc(), Staff.external_account.asc())
        )
    ).scalars().all()
    managed_staff = [
        managed_item
        for managed_item in managed_staff
        if _managed_badge_target_visible(user, staff, managed_item)
    ]

    devices_by_staff = await _load_latest_devices_by_staff(db, [managed_item.id for managed_item in managed_staff])
    position_names = await _load_position_names(db, managed_staff)
    remote_by_code, warning_by_code = await _load_managed_remote_devices(list(devices_by_staff.values()))

    contexts: list[_MyBadgeContext] = []
    items: list[MyBadgeOut] = []
    for managed_item in managed_staff:
        device = devices_by_staff.get(managed_item.id)
        reason = None if device is not None else "当前员工暂未绑定工牌"
        device_code = _clean_text(device.device_code) if device is not None else None
        context = _build_managed_badge_context(
            staff=managed_item,
            device=device,
            position_name=position_names.get(managed_item.position_id or ""),
            remote_device=remote_by_code.get(device_code or ""),
            remote_warning=warning_by_code.get(device_code or ""),
        )
        contexts.append(context)
        items.append(_build_my_badge_out(context, reason=reason))
    await _refresh_managed_device_cache(db, contexts)
    return items


@router.put("/me", response_model=AccountProfileOut)
async def update_account_profile(
    body: AccountProfileUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    next_name = body.display_name.strip()
    if not next_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "显示名称不能为空")

    if user.display_name != next_name:
        user.display_name = next_name
        await db.commit()
        await db.refresh(user)
        await append_audit_log(
            db,
            operator_name=user.display_name or user.username,
            ip_address=request.client.host if request.client else "",
            module_name="个人中心",
            action_name="更新资料",
            content=f"更新显示名称为 {next_name}",
        )

    return await _build_account_profile_out(db, user)


@router.post("/change-password", response_model=MessageOut)
async def change_account_password(
    body: ChangePasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not verify_password(body.current_password, user.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "当前密码错误")
    if body.current_password == body.new_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码不能与当前密码相同")

    user.hashed_password = hash_password(body.new_password)
    await db.commit()
    await db.refresh(user)
    await append_audit_log(
        db,
        operator_name=user.display_name or user.username,
        ip_address=request.client.host if request.client else "",
        module_name="个人中心",
        action_name="修改密码",
        content="修改登录密码",
    )
    return MessageOut(message="密码已更新")


@router.post("/my-badge/recording/start", response_model=MessageOut)
async def start_my_badge_recording(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = await _require_my_badge_recording_context(db, user)
    try:
        if context.remote_provider == "iot":
            await iot_control_recording(action="start", device_no=context.device.device_code)
        else:
            await dvi_control_recording(
                action="start",
                team_code=context.team_code or "",
                user_id=context.user_id or "",
            )
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle_dingtalk_error(exc) from exc

    await append_audit_log(
        db,
        operator_name=user.display_name or user.username,
        ip_address=request.client.host if request.client else "",
        module_name="我的工牌",
        action_name="开始录音",
        content=f"开始录音：工牌 {context.device.device_code}",
    )
    return MessageOut(message="录音已启动")


@router.post("/my-badge/recording/stop", response_model=MessageOut)
async def stop_my_badge_recording(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = await _require_my_badge_recording_context(db, user)
    try:
        if context.remote_provider == "iot":
            await iot_control_recording(action="stop", device_no=context.device.device_code)
        else:
            await dvi_control_recording(
                action="stop",
                team_code=context.team_code or "",
                user_id=context.user_id or "",
            )
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle_dingtalk_error(exc) from exc

    await append_audit_log(
        db,
        operator_name=user.display_name or user.username,
        ip_address=request.client.host if request.client else "",
        module_name="我的工牌",
        action_name="暂停录音",
        content=f"暂停录音：工牌 {context.device.device_code}",
    )
    return MessageOut(message="录音已暂停")
