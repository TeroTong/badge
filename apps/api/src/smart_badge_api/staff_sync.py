from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.audit import append_audit_log
from smart_badge_api.core.config import get_settings
from smart_badge_api.core.permissions import LEGACY_STAFF_PERMISSION_ROLE_MAP, normalize_permission_role
from smart_badge_api.db.models import PositionProfile, Staff
from smart_badge_api.db.session import _session_factory
from smart_badge_api.db.system_defaults import ensure_system_management_defaults
from smart_badge_api.periodic_locks import STAFF_DIRECTORY_REFRESH_LOCK_ID, periodic_advisory_lock

logger = logging.getLogger("smart_badge.staff_sync")
DEFAULT_POSITION_NAME = "普通员工"
STAFF_DIRECTORY_SYNC_AUDIT_MODULE_NAME = "人员管理"
STAFF_DIRECTORY_SYNC_AUDIT_ACTION_NAME = "定时同步员工状态"
SYSTEM_SCHEDULER_OPERATOR_NAME = "系统定时任务"
SYSTEM_SCHEDULER_IP_ADDRESS = "scheduler"


@dataclass(slots=True)
class StaffDirectoryRecord:
    advisor_code: str
    name: str | None
    hospital_code: str | None
    hospital_short_name: str | None
    is_doctor: bool = False
    is_nurse: bool = False
    is_anesthetist: bool = False
    is_cashier: bool = False
    is_guide: bool = False
    is_pre_advisor: bool = False
    is_onsite_advisor: bool = False
    is_advisor_assistant: bool = False
    is_doctor_assistant: bool = False
    is_vip_service: bool = False
    is_left: bool = False


@dataclass(slots=True)
class StaffDirectoryRefreshResult:
    checked_count: int = 0
    updated_count: int = 0
    missing_count: int = 0
    deactivated_count: int = 0


def _resolve_staff_directory_dsn(staff_directory_dsn: str | None = None) -> str:
    return (staff_directory_dsn or get_settings().resolved_staff_directory_dsn).strip()


def _flag_is_x(value: object) -> bool:
    return str(value or "").strip().upper() == "X"


def _derive_staff_role(current_role: str | None, directory_record: StaffDirectoryRecord) -> str:
    if directory_record.is_doctor:
        return "doctor"
    return "consultant"


def _apply_directory_record(
    staff: Staff,
    directory_record: StaffDirectoryRecord,
    *,
    default_position: PositionProfile | None = None,
) -> tuple[bool, bool]:
    changed = False
    deactivated = False
    legacy_permission_role = normalize_permission_role(
        getattr(staff, "permission_role", None) or LEGACY_STAFF_PERMISSION_ROLE_MAP.get(staff.role, "staff")
    )

    if directory_record.name and staff.name != directory_record.name:
        staff.name = directory_record.name
        changed = True
    if staff.hospital_code != directory_record.hospital_code:
        staff.hospital_code = directory_record.hospital_code
        changed = True
    if staff.hospital_short_name != directory_record.hospital_short_name:
        staff.hospital_short_name = directory_record.hospital_short_name
        changed = True

    if staff.external_account != directory_record.advisor_code:
        staff.external_account = directory_record.advisor_code
        changed = True

    next_role = _derive_staff_role(staff.role, directory_record)
    if staff.role != next_role:
        staff.role = next_role
        changed = True

    next_permission_role = legacy_permission_role
    if getattr(staff, "permission_role", None) != next_permission_role:
        staff.permission_role = next_permission_role
        changed = True

    if default_position and next_role == default_position.mapped_role and not staff.position_id:
        staff.position_id = default_position.id
        changed = True

    next_active = not directory_record.is_left
    if staff.is_active != next_active:
        if staff.is_active and not next_active:
            deactivated = True
        staff.is_active = next_active
        changed = True

    for flag in (
        "is_doctor",
        "is_nurse",
        "is_anesthetist",
        "is_cashier",
        "is_guide",
        "is_pre_advisor",
        "is_onsite_advisor",
        "is_advisor_assistant",
        "is_doctor_assistant",
        "is_vip_service",
    ):
        remote_value = getattr(directory_record, flag)
        if getattr(staff, flag) != remote_value:
            setattr(staff, flag, remote_value)
            changed = True

    return changed, deactivated


def _build_staff_directory_refresh_log_payload(
    status: Literal["success", "failed"],
    *,
    result: StaffDirectoryRefreshResult | None = None,
    error_message: str | None = None,
) -> str:
    normalized_error = None
    if error_message:
        normalized_error = error_message.strip()[:500] or None
    summary = None
    if result is not None:
        summary = (
            f"检查 {result.checked_count} 人，更新 {result.updated_count} 人，"
            f"停用 {result.deactivated_count} 人，未匹配 {result.missing_count} 人"
        )
    elif normalized_error:
        summary = normalized_error

    payload: dict[str, Any] = {
        "status": status,
        "summary": summary,
        "checked_count": result.checked_count if result is not None else None,
        "updated_count": result.updated_count if result is not None else None,
        "missing_count": result.missing_count if result is not None else None,
        "deactivated_count": result.deactivated_count if result is not None else None,
        "error_message": normalized_error,
    }
    return json.dumps(payload, ensure_ascii=False)


def parse_staff_directory_refresh_log_payload(content: str) -> dict[str, Any]:
    if not content:
        return {}
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def _append_staff_directory_refresh_audit_log(
    db: AsyncSession,
    *,
    status: Literal["success", "failed"],
    result: StaffDirectoryRefreshResult | None = None,
    error_message: str | None = None,
) -> None:
    await append_audit_log(
        db,
        operator_name=SYSTEM_SCHEDULER_OPERATOR_NAME,
        ip_address=SYSTEM_SCHEDULER_IP_ADDRESS,
        module_name=STAFF_DIRECTORY_SYNC_AUDIT_MODULE_NAME,
        action_name=STAFF_DIRECTORY_SYNC_AUDIT_ACTION_NAME,
        content=_build_staff_directory_refresh_log_payload(
            status,
            result=result,
            error_message=error_message,
        ),
    )


async def record_staff_directory_refresh_failure(error: Exception) -> None:
    error_message = str(error).strip()
    if error_message:
        error_message = f"{type(error).__name__}: {error_message}"
    else:
        error_message = type(error).__name__

    try:
        async with _session_factory() as db:
            await _append_staff_directory_refresh_audit_log(
                db,
                status="failed",
                error_message=error_message,
            )
    except Exception:
        logger.exception("failed to persist staff directory refresh failure status")


def lookup_staff_directory_records(
    advisor_codes: Iterable[str],
    *,
    staff_directory_dsn: str | None = None,
) -> dict[str, StaffDirectoryRecord]:
    normalized_codes = sorted({code.strip() for code in advisor_codes if code and code.strip()})
    if not normalized_codes:
        return {}

    dsn = _resolve_staff_directory_dsn(staff_directory_dsn)
    if not dsn:
        return {}

    try:
        import psycopg
    except ImportError:
        return {}

    lookup_columns = [
        "ygid", "ygnam", "yybm", "yydm", "yyjc",
        "doctor", "nurse", "mazui", "cashx", "daoyi",
        "zgwyq", "zgwxc", "zgwzl", "yizhu", "vipkf", "lzflg",
    ]
    records: dict[str, StaffDirectoryRecord] = {}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select column_name from information_schema.columns "
                "where table_schema = 'cur' and table_name = 'staff' and column_name = any(%s)",
                (lookup_columns,),
            )
            column_names = {str(row[0]).strip().lower() for row in cur.fetchall()}
            if "ygid" not in column_names or "ygnam" not in column_names:
                return {}

            hospital_code_column = "yybm" if "yybm" in column_names else "yydm" if "yydm" in column_names else None
            selected_columns = ["ygid", "ygnam"]
            if hospital_code_column:
                selected_columns.append(hospital_code_column)
            if "yyjc" in column_names:
                selected_columns.append("yyjc")
            role_columns = [
                column
                for column in (
                    "doctor", "nurse", "mazui", "cashx", "daoyi",
                    "zgwyq", "zgwxc", "zgwzl", "yizhu", "vipkf",
                )
                if column in column_names
            ]
            selected_columns.extend(role_columns)
            if "lzflg" in column_names:
                selected_columns.append("lzflg")

            cur.execute(
                f"select {', '.join(selected_columns)} from cur.staff where ygid = any(%s)",
                (normalized_codes,),
            )
            for row in cur.fetchall():
                data = {column: row[index] for index, column in enumerate(selected_columns)}
                advisor_code = str(data.get("ygid") or "").strip()
                if not advisor_code:
                    continue
                records[advisor_code] = StaffDirectoryRecord(
                    advisor_code=advisor_code,
                    name=str(data.get("ygnam")).strip() if data.get("ygnam") else None,
                    hospital_code=str(data.get(hospital_code_column or "") or "").strip() or None,
                    hospital_short_name=str(data.get("yyjc") or "").strip() or None,
                    is_doctor=_flag_is_x(data.get("doctor")),
                    is_nurse=_flag_is_x(data.get("nurse")),
                    is_anesthetist=_flag_is_x(data.get("mazui")),
                    is_cashier=_flag_is_x(data.get("cashx")),
                    is_guide=_flag_is_x(data.get("daoyi")),
                    is_pre_advisor=_flag_is_x(data.get("zgwyq")),
                    is_onsite_advisor=_flag_is_x(data.get("zgwxc")),
                    is_advisor_assistant=_flag_is_x(data.get("zgwzl")),
                    is_doctor_assistant=_flag_is_x(data.get("yizhu")),
                    is_vip_service=_flag_is_x(data.get("vipkf")),
                    is_left=_flag_is_x(data.get("lzflg")),
                )

    return records


async def sync_all_staff_from_directory(
    db: AsyncSession,
    *,
    staff_directory_dsn: str | None = None,
) -> StaffDirectoryRefreshResult:
    await ensure_system_management_defaults(db)

    staff_rows = (
        await db.execute(select(Staff).where(Staff.external_account.is_not(None)))
    ).scalars().all()
    result = StaffDirectoryRefreshResult(checked_count=len(staff_rows))
    if not staff_rows:
        return result

    advisor_codes = [row.external_account for row in staff_rows if row.external_account]
    directory_records = await asyncio.to_thread(
        lookup_staff_directory_records,
        advisor_codes,
        staff_directory_dsn=staff_directory_dsn,
    )
    positions = {item.name: item for item in (await db.execute(select(PositionProfile))).scalars().all()}
    default_position = positions.get(DEFAULT_POSITION_NAME)

    for staff in staff_rows:
        advisor_code = (staff.external_account or "").strip()
        if not advisor_code:
            continue
        directory_record = directory_records.get(advisor_code)
        if directory_record is None:
            result.missing_count += 1
            continue
        changed, deactivated = _apply_directory_record(staff, directory_record, default_position=default_position)
        if changed:
            result.updated_count += 1
        if deactivated:
            result.deactivated_count += 1

    if result.updated_count:
        await db.commit()

    return result


async def run_staff_directory_refresh_once(
    *,
    staff_directory_dsn: str | None = None,
) -> StaffDirectoryRefreshResult:
    async with _session_factory() as db:
        result = await sync_all_staff_from_directory(db, staff_directory_dsn=staff_directory_dsn)
        try:
            await _append_staff_directory_refresh_audit_log(
                db,
                status="success",
                result=result,
            )
        except Exception:
            logger.exception("failed to persist staff directory refresh success status")
        return result


async def periodic_staff_directory_refresh(
    stop_event: asyncio.Event | None = None,
    *,
    staff_directory_dsn: str | None = None,
) -> None:
    settings = get_settings()
    interval_seconds = settings.staff_refresh_interval_seconds
    if interval_seconds <= 0:
        logger.info("periodic staff directory refresh disabled because interval <= 0")
        return

    resolved_dsn = _resolve_staff_directory_dsn(staff_directory_dsn)
    if not resolved_dsn:
        logger.warning("periodic staff directory refresh disabled because no staff directory DSN is configured")
        return

    while True:
        try:
            async with periodic_advisory_lock("staff_directory_refresh", STAFF_DIRECTORY_REFRESH_LOCK_ID) as acquired:
                if acquired:
                    result = await run_staff_directory_refresh_once(staff_directory_dsn=resolved_dsn)
                    logger.info(
                        "staff directory refresh checked=%d updated=%d deactivated=%d missing=%d",
                        result.checked_count,
                        result.updated_count,
                        result.deactivated_count,
                        result.missing_count,
                    )
        except Exception as exc:
            logger.exception("staff directory refresh failed")
            await record_staff_directory_refresh_failure(exc)

        if stop_event is None:
            await asyncio.sleep(interval_seconds)
            continue

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            break
        except asyncio.TimeoutError:
            continue
