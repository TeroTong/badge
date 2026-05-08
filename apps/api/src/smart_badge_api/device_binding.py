from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.db.models import Device, DeviceStaffBinding, Recording, Staff

EARLIEST_BINDING_AT = datetime(1900, 1, 1, tzinfo=timezone.utc)


class DeviceBindingOverlapError(ValueError):
    def __init__(
        self,
        *,
        device_code: str,
        requested_start: datetime,
        requested_end: datetime | None,
        conflicts: list[dict[str, str | None]],
    ) -> None:
        self.device_code = device_code
        self.requested_start = requested_start
        self.requested_end = requested_end
        self.conflicts = conflicts
        super().__init__("所选绑定时间段与该工牌已有绑定重叠，请确认是否以新的绑定时间段为准")

    def as_detail(self) -> dict[str, Any]:
        return {
            "code": "device_binding_overlap",
            "message": str(self),
            "deviceCode": self.device_code,
            "requestedStart": _iso_or_none(self.requested_start),
            "requestedEnd": _iso_or_none(self.requested_end),
            "conflicts": self.conflicts,
        }


def clean_device_code(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _coerce_effective_datetime(
    value: datetime | str | None,
    *,
    field_label: str,
    default_now: bool = False,
) -> datetime | None:
    if isinstance(value, datetime):
        resolved = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            resolved = datetime.now(timezone.utc) if default_now else None
        else:
            try:
                resolved = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{field_label}格式不正确，请使用 ISO 时间") from exc
    elif value is None:
        resolved = datetime.now(timezone.utc) if default_now else None
    else:
        raise ValueError(f"{field_label}格式不正确，请使用 ISO 时间")

    if resolved is None:
        return None
    if resolved.tzinfo is None:
        return resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def _coerce_lookup_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    return _coerce_effective_datetime(value, field_label="时间", default_now=False)


def _coerce_binding_range(
    *,
    effective_start: datetime | str | None,
    effective_end: datetime | str | None,
) -> tuple[datetime, datetime | None]:
    start_at = _coerce_effective_datetime(
        effective_start,
        field_label="绑定开始时间",
        default_now=False,
    )
    end_at = _coerce_effective_datetime(
        effective_end,
        field_label="绑定结束时间",
        default_now=False,
    )

    normalized_start = start_at or EARLIEST_BINDING_AT
    if end_at is not None and end_at <= normalized_start:
        raise ValueError("绑定结束时间必须晚于开始时间")
    return normalized_start, end_at


def _range_overlaps(
    left_start: datetime,
    left_end: datetime | None,
    right_start: datetime,
    right_end: datetime | None,
) -> bool:
    left_ends_after_right_starts = left_end is None or left_end > right_start
    right_ends_after_left_starts = right_end is None or right_end > left_start
    return left_ends_after_right_starts and right_ends_after_left_starts


def _max_range_end(left_end: datetime | None, right_end: datetime | None) -> datetime | None:
    if left_end is None or right_end is None:
        return None
    return max(left_end, right_end)


def _ranges_touch_or_overlap(
    left_start: datetime,
    left_end: datetime | None,
    right_start: datetime,
    right_end: datetime | None,
) -> bool:
    if _range_overlaps(left_start, left_end, right_start, right_end):
        return True
    if left_end is not None and left_end == right_start:
        return True
    if right_end is not None and right_end == left_start:
        return True
    return False


async def get_device_by_code(db: AsyncSession, device_code: str) -> Device | None:
    normalized_code = clean_device_code(device_code)
    if not normalized_code:
        return None
    return (
        await db.execute(select(Device).where(Device.device_code == normalized_code).limit(1))
    ).scalar_one_or_none()


async def load_device_staff_history(
    db: AsyncSession,
    device_codes: Iterable[str],
) -> dict[str, list[dict[str, object | None]]]:
    normalized_codes = [
        code
        for code in dict.fromkeys(clean_device_code(item) for item in device_codes)
        if code
    ]
    if not normalized_codes:
        return {}

    device_rows = (
        await db.execute(
            select(
                Device.id.label("device_id"),
                Device.device_code.label("device_code"),
                Device.staff_id.label("current_staff_id"),
                Staff.name.label("current_staff_name"),
                Staff.role.label("current_staff_role"),
                Staff.hospital_code.label("current_staff_hospital_code"),
                Staff.hospital_short_name.label("current_staff_hospital_short_name"),
            )
            .select_from(Device)
            .join(Staff, Staff.id == Device.staff_id, isouter=True)
            .where(Device.device_code.in_(normalized_codes))
        )
    ).all()

    history_rows = (
        await db.execute(
            select(
                Device.device_code.label("device_code"),
                DeviceStaffBinding.staff_id.label("staff_id"),
                Staff.name.label("staff_name"),
                Staff.role.label("staff_role"),
                Staff.hospital_code.label("staff_hospital_code"),
                Staff.hospital_short_name.label("staff_hospital_short_name"),
                DeviceStaffBinding.effective_from.label("effective_from"),
                DeviceStaffBinding.effective_to.label("effective_to"),
            )
            .select_from(DeviceStaffBinding)
            .join(Device, Device.id == DeviceStaffBinding.device_id)
            .join(Staff, Staff.id == DeviceStaffBinding.staff_id, isouter=True)
            .where(Device.device_code.in_(normalized_codes))
            .order_by(
                Device.device_code.asc(),
                DeviceStaffBinding.effective_from.asc(),
                DeviceStaffBinding.created_at.asc(),
            )
        )
    ).all()

    history_by_code: dict[str, list[dict[str, object | None]]] = {
        code: [] for code in normalized_codes
    }
    for row in history_rows:
        device_code = clean_device_code(row.device_code)
        if not device_code:
            continue
        history_by_code.setdefault(device_code, []).append(
            {
                "staff_id": clean_device_code(row.staff_id),
                "staff_name": clean_device_code(row.staff_name),
                "staff_role": clean_device_code(row.staff_role),
                "staff_hospital_code": clean_device_code(row.staff_hospital_code),
                "staff_hospital_short_name": clean_device_code(row.staff_hospital_short_name),
                "effective_from": _coerce_lookup_datetime(row.effective_from),
                "effective_to": _coerce_lookup_datetime(row.effective_to),
            }
        )

    for row in device_rows:
        device_code = clean_device_code(row.device_code)
        if not device_code:
            continue
        current_staff_id = clean_device_code(row.current_staff_id)
        if history_by_code.get(device_code) or not current_staff_id:
            continue
        history_by_code[device_code] = [
            {
                "staff_id": current_staff_id,
                "staff_name": clean_device_code(row.current_staff_name),
                "staff_role": clean_device_code(row.current_staff_role),
                "staff_hospital_code": clean_device_code(row.current_staff_hospital_code),
                "staff_hospital_short_name": clean_device_code(row.current_staff_hospital_short_name),
                "effective_from": EARLIEST_BINDING_AT,
                "effective_to": None,
            }
        ]

    return history_by_code


def resolve_device_staff_binding(
    history_by_code: dict[str, list[dict[str, object | None]]],
    *,
    device_code: str | None,
    occurred_at: datetime | str | None,
) -> dict[str, str | None] | None:
    normalized_code = clean_device_code(device_code)
    if not normalized_code:
        return None

    rows = history_by_code.get(normalized_code) or []
    if not rows:
        return None

    lookup_at = _coerce_lookup_datetime(occurred_at) or datetime.now(timezone.utc)
    for row in reversed(rows):
        effective_from = _coerce_lookup_datetime(row.get("effective_from"))
        effective_to = _coerce_lookup_datetime(row.get("effective_to"))
        if effective_from and lookup_at < effective_from:
            continue
        if effective_to and lookup_at >= effective_to:
            continue
        return {
            "staff_id": clean_device_code(row.get("staff_id")),
            "staff_name": clean_device_code(row.get("staff_name")),
            "staff_role": clean_device_code(row.get("staff_role")),
            "staff_hospital_code": clean_device_code(row.get("staff_hospital_code")),
            "staff_hospital_short_name": clean_device_code(row.get("staff_hospital_short_name")),
        }

    return None


async def refresh_recording_staff_assignments_for_device(
    db: AsyncSession,
    *,
    device: Device,
    touched_from: datetime | None = None,
) -> int:
    normalized_code = clean_device_code(device.device_code)
    if not normalized_code:
        return 0

    history_by_code = await load_device_staff_history(db, [normalized_code])
    valid_staff_ids = {
        clean_device_code(entry.get("staff_id"))
        for entry in history_by_code.get(normalized_code, [])
        if clean_device_code(entry.get("staff_id"))
    }

    stmt = select(Recording).where(
        or_(
            Recording.device_id == device.id,
            Recording.device_id == normalized_code,
        )
    )
    if touched_from is not None:
        stmt = stmt.where(Recording.created_at >= touched_from)

    recordings = (await db.execute(stmt)).scalars().all()
    updated = 0
    for recording in recordings:
        resolved = resolve_device_staff_binding(
            history_by_code,
            device_code=normalized_code,
            occurred_at=recording.created_at,
        )
        resolved_staff_id = clean_device_code((resolved or {}).get("staff_id"))
        if recording.staff_id and valid_staff_ids and recording.staff_id not in valid_staff_ids and not resolved_staff_id:
            continue
        if recording.staff_id != resolved_staff_id:
            recording.staff_id = resolved_staff_id
            updated += 1
    return updated


async def _ensure_legacy_device_binding_rows(db: AsyncSession, *, device: Device) -> None:
    if not clean_device_code(device.staff_id):
        return
    existing = (
        await db.execute(
            select(DeviceStaffBinding.id)
            .where(DeviceStaffBinding.device_id == device.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    db.add(
        DeviceStaffBinding(
            device_id=device.id,
            staff_id=device.staff_id,
            effective_from=EARLIEST_BINDING_AT,
            effective_to=None,
        )
    )
    await db.flush()


async def _cut_binding_row(
    db: AsyncSession,
    *,
    row: DeviceStaffBinding,
    cut_start: datetime,
    cut_end: datetime | None,
) -> None:
    existing_start = _coerce_lookup_datetime(row.effective_from) or EARLIEST_BINDING_AT
    existing_end = _coerce_lookup_datetime(row.effective_to)
    if not _range_overlaps(existing_start, existing_end, cut_start, cut_end):
        return

    has_left = existing_start < cut_start
    has_right = cut_end is not None and (existing_end is None or cut_end < existing_end)

    if has_left and has_right:
        original_end = existing_end
        row.effective_to = cut_start
        db.add(
            DeviceStaffBinding(
                device_id=row.device_id,
                staff_id=row.staff_id,
                effective_from=cut_end,
                effective_to=original_end,
            )
        )
        return

    if has_left:
        row.effective_to = cut_start
        return

    if has_right:
        row.effective_from = cut_end
        return

    await db.delete(row)


async def _merge_device_binding_rows(db: AsyncSession, *, device_id: str) -> None:
    rows = (
        await db.execute(
            select(DeviceStaffBinding)
            .where(DeviceStaffBinding.device_id == device_id)
            .order_by(
                DeviceStaffBinding.effective_from.asc(),
                DeviceStaffBinding.created_at.asc(),
                DeviceStaffBinding.id.asc(),
            )
        )
    ).scalars().all()

    previous: DeviceStaffBinding | None = None
    for row in rows:
        current_start = _coerce_lookup_datetime(row.effective_from) or EARLIEST_BINDING_AT
        current_end = _coerce_lookup_datetime(row.effective_to)
        if previous is None:
            previous = row
            continue

        previous_start = _coerce_lookup_datetime(previous.effective_from) or EARLIEST_BINDING_AT
        previous_end = _coerce_lookup_datetime(previous.effective_to)
        if previous.staff_id == row.staff_id and _ranges_touch_or_overlap(
            previous_start,
            previous_end,
            current_start,
            current_end,
        ):
            previous.effective_from = min(previous_start, current_start)
            previous.effective_to = _max_range_end(previous_end, current_end)
            await db.delete(row)
            continue

        previous = row


async def _sync_current_binding_pointers(
    db: AsyncSession,
    *,
    device_ids: Iterable[str],
    staff_ids: Iterable[str],
) -> None:
    now = datetime.now(timezone.utc)
    normalized_device_ids = [item for item in dict.fromkeys(device_ids) if item]
    normalized_staff_ids = [item for item in dict.fromkeys(staff_ids) if item]

    if normalized_device_ids:
        devices = (
            await db.execute(select(Device).where(Device.id.in_(normalized_device_ids)))
        ).scalars().all()
        history_by_code = await load_device_staff_history(db, [device.device_code for device in devices])
        for device in devices:
            resolved = resolve_device_staff_binding(
                history_by_code,
                device_code=device.device_code,
                occurred_at=now,
            )
            device.staff_id = clean_device_code((resolved or {}).get("staff_id"))

    if normalized_staff_ids:
        rows = (
            await db.execute(
                select(
                    DeviceStaffBinding.staff_id,
                    Device.device_code,
                    DeviceStaffBinding.effective_from,
                    DeviceStaffBinding.created_at,
                )
                .select_from(DeviceStaffBinding)
                .join(Device, Device.id == DeviceStaffBinding.device_id)
                .where(
                    DeviceStaffBinding.staff_id.in_(normalized_staff_ids),
                    DeviceStaffBinding.effective_from <= now,
                    or_(
                        DeviceStaffBinding.effective_to.is_(None),
                        DeviceStaffBinding.effective_to > now,
                    ),
                )
                .order_by(
                    DeviceStaffBinding.staff_id.asc(),
                    DeviceStaffBinding.effective_from.desc(),
                    DeviceStaffBinding.created_at.desc(),
                )
            )
        ).all()

        preferred_badge_by_staff: dict[str, str] = {}
        for row in rows:
            staff_id = clean_device_code(row.staff_id)
            device_code = clean_device_code(row.device_code)
            if staff_id and device_code and staff_id not in preferred_badge_by_staff:
                preferred_badge_by_staff[staff_id] = device_code

        staffs = (
            await db.execute(select(Staff).where(Staff.id.in_(normalized_staff_ids)))
        ).scalars().all()
        for person in staffs:
            person.badge_id = preferred_badge_by_staff.get(person.id)


def _serialize_overlap_conflict(
    *,
    row: DeviceStaffBinding,
    staff_name: str | None,
) -> dict[str, str | None]:
    return {
        "staffId": clean_device_code(row.staff_id),
        "staffName": clean_device_code(staff_name),
        "effectiveStart": _iso_or_none(_coerce_lookup_datetime(row.effective_from)),
        "effectiveEnd": _iso_or_none(_coerce_lookup_datetime(row.effective_to)),
    }


async def bind_staff_to_device(
    db: AsyncSession,
    *,
    staff: Staff,
    device_code: str,
    device_name: str | None = None,
    effective_start: datetime | str | None = None,
    effective_end: datetime | str | None = None,
    override_overlap: bool = False,
    effective_from: datetime | str | None = None,
) -> Device:
    normalized_code = clean_device_code(device_code)
    if not normalized_code:
        raise ValueError("device_code is required")

    requested_start = effective_start if effective_start is not None else effective_from
    if effective_start is None and effective_from is not None and not override_overlap:
        override_overlap = True
    start_at, end_at = _coerce_binding_range(
        effective_start=requested_start,
        effective_end=effective_end,
    )

    target_device = await get_device_by_code(db, normalized_code)
    staff_hospital_code = clean_device_code(staff.hospital_code)
    staff_hospital_short_name = clean_device_code(staff.hospital_short_name)
    if target_device is None:
        target_device = Device(
            name=device_name or normalized_code,
            device_code=normalized_code,
            staff_id=None,
            hospital_code=staff_hospital_code,
            hospital_short_name=staff_hospital_short_name,
            status="offline",
            is_active=True,
        )
        db.add(target_device)
        await db.flush()
    else:
        if device_name and target_device.name != device_name:
            target_device.name = device_name
        target_device.is_active = True
        await _ensure_legacy_device_binding_rows(db, device=target_device)

    device_hospital_code = clean_device_code(target_device.hospital_code)
    if device_hospital_code and staff_hospital_code and device_hospital_code != staff_hospital_code:
        raise ValueError("工牌归属机构与人员所属机构不一致，请先调整工牌机构归属")
    if staff_hospital_code and not device_hospital_code:
        target_device.hospital_code = staff_hospital_code
    if staff_hospital_short_name and not clean_device_code(target_device.hospital_short_name):
        target_device.hospital_short_name = staff_hospital_short_name

    binding_rows = (
        await db.execute(
            select(DeviceStaffBinding, Staff.name)
            .select_from(DeviceStaffBinding)
            .join(Staff, Staff.id == DeviceStaffBinding.staff_id, isouter=True)
            .where(DeviceStaffBinding.device_id == target_device.id)
            .order_by(
                DeviceStaffBinding.effective_from.asc(),
                DeviceStaffBinding.created_at.asc(),
                DeviceStaffBinding.id.asc(),
            )
        )
    ).all()

    conflicts: list[dict[str, str | None]] = []
    affected_staff_ids = {staff.id}
    for row, staff_name in binding_rows:
        row_start = _coerce_lookup_datetime(row.effective_from) or EARLIEST_BINDING_AT
        row_end = _coerce_lookup_datetime(row.effective_to)
        if not _range_overlaps(row_start, row_end, start_at, end_at):
            continue
        affected_staff_ids.add(row.staff_id)
        if row.staff_id != staff.id:
            conflicts.append(_serialize_overlap_conflict(row=row, staff_name=staff_name))

    if conflicts and not override_overlap:
        raise DeviceBindingOverlapError(
            device_code=normalized_code,
            requested_start=start_at,
            requested_end=end_at,
            conflicts=conflicts,
        )

    if conflicts:
        for row, _staff_name in binding_rows:
            if row.staff_id == staff.id:
                continue
            await _cut_binding_row(
                db,
                row=row,
                cut_start=start_at,
                cut_end=end_at,
            )

    matching_same_start = next(
        (
            row
            for row, _staff_name in binding_rows
            if row.staff_id == staff.id
            and (_coerce_lookup_datetime(row.effective_from) or EARLIEST_BINDING_AT) == start_at
        ),
        None,
    )
    if matching_same_start is not None:
        matching_same_start.effective_to = _max_range_end(
            _coerce_lookup_datetime(matching_same_start.effective_to),
            end_at,
        )
    else:
        db.add(
            DeviceStaffBinding(
                device_id=target_device.id,
                staff_id=staff.id,
                effective_from=start_at,
                effective_to=end_at,
            )
        )

    await db.flush()
    await _merge_device_binding_rows(db, device_id=target_device.id)
    await db.flush()
    await refresh_recording_staff_assignments_for_device(
        db,
        device=target_device,
        touched_from=None if start_at == EARLIEST_BINDING_AT else start_at,
    )
    await _sync_current_binding_pointers(
        db,
        device_ids=[target_device.id],
        staff_ids=affected_staff_ids,
    )

    await db.commit()
    await db.refresh(staff)
    await db.refresh(target_device)
    return target_device


async def unbind_device_from_staff(
    db: AsyncSession,
    *,
    device_code: str,
    effective_from: datetime | str | None = None,
) -> Device | None:
    start_at = _coerce_effective_datetime(
        effective_from,
        field_label="解绑开始时间",
        default_now=True,
    )
    assert start_at is not None

    target_device = await get_device_by_code(db, device_code)
    if target_device is None:
        return None
    await _ensure_legacy_device_binding_rows(db, device=target_device)

    rows = (
        await db.execute(
            select(DeviceStaffBinding)
            .where(DeviceStaffBinding.device_id == target_device.id)
            .order_by(
                DeviceStaffBinding.effective_from.asc(),
                DeviceStaffBinding.created_at.asc(),
                DeviceStaffBinding.id.asc(),
            )
        )
    ).scalars().all()
    affected_staff_ids = {clean_device_code(row.staff_id) for row in rows if clean_device_code(row.staff_id)}

    for row in rows:
        await _cut_binding_row(
            db,
            row=row,
            cut_start=start_at,
            cut_end=None,
        )

    await db.flush()
    await _merge_device_binding_rows(db, device_id=target_device.id)
    await db.flush()
    await refresh_recording_staff_assignments_for_device(db, device=target_device, touched_from=start_at)
    await _sync_current_binding_pointers(
        db,
        device_ids=[target_device.id],
        staff_ids=affected_staff_ids,
    )

    await db.commit()
    await db.refresh(target_device)
    return target_device


async def clear_device_staff_history(
    db: AsyncSession,
    *,
    device_code: str,
    clear_recording_owners: bool = True,
) -> Device | None:
    target_device = await get_device_by_code(db, device_code)
    if target_device is None:
        return None

    await _ensure_legacy_device_binding_rows(db, device=target_device)
    rows = (
        await db.execute(
            select(DeviceStaffBinding).where(DeviceStaffBinding.device_id == target_device.id)
        )
    ).scalars().all()
    affected_staff_ids = {
        clean_device_code(row.staff_id)
        for row in rows
        if clean_device_code(row.staff_id)
    }
    if clean_device_code(target_device.staff_id):
        affected_staff_ids.add(clean_device_code(target_device.staff_id))

    bound_staff_rows = (
        await db.execute(select(Staff).where(Staff.badge_id == target_device.device_code))
    ).scalars().all()
    for person in bound_staff_rows:
        person.badge_id = None
        affected_staff_ids.add(person.id)

    for row in rows:
        await db.delete(row)

    target_device.staff_id = None
    await db.flush()
    if clear_recording_owners:
        await refresh_recording_staff_assignments_for_device(db, device=target_device, touched_from=None)
    await _sync_current_binding_pointers(
        db,
        device_ids=[target_device.id],
        staff_ids=[item for item in affected_staff_ids if item],
    )

    await db.commit()
    await db.refresh(target_device)
    return target_device


async def unbind_staff_badge(
    db: AsyncSession,
    *,
    staff: Staff,
    effective_from: datetime | str | None = None,
) -> None:
    start_at = _coerce_effective_datetime(
        effective_from,
        field_label="解绑开始时间",
        default_now=True,
    )
    assert start_at is not None

    bound_devices = (
        await db.execute(select(Device).where(Device.staff_id == staff.id))
    ).scalars().all()
    for device in bound_devices:
        await _ensure_legacy_device_binding_rows(db, device=device)

    rows = (
        await db.execute(
            select(DeviceStaffBinding)
            .where(DeviceStaffBinding.staff_id == staff.id)
            .order_by(
                DeviceStaffBinding.device_id.asc(),
                DeviceStaffBinding.effective_from.asc(),
                DeviceStaffBinding.created_at.asc(),
                DeviceStaffBinding.id.asc(),
            )
        )
    ).scalars().all()
    affected_device_ids = {row.device_id for row in rows}

    for row in rows:
        await _cut_binding_row(
            db,
            row=row,
            cut_start=start_at,
            cut_end=None,
        )

    await db.flush()

    touched_devices = (
        await db.execute(select(Device).where(Device.id.in_(affected_device_ids)))
    ).scalars().all()
    for device in touched_devices:
        await _merge_device_binding_rows(db, device_id=device.id)
    await db.flush()
    for device in touched_devices:
        await refresh_recording_staff_assignments_for_device(db, device=device, touched_from=start_at)

    await _sync_current_binding_pointers(
        db,
        device_ids=affected_device_ids,
        staff_ids=[staff.id],
    )

    await db.commit()
    await db.refresh(staff)
