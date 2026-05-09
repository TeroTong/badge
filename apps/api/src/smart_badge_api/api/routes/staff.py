from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from functools import lru_cache
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.account_provisioning import (
    AccountProvisioningError,
    get_linked_user_by_staff_id,
    provision_staff_account,
    reset_staff_account_password,
    set_staff_account_active,
    sync_user_scope_from_staff,
)
from smart_badge_api.api.audit import append_audit_log
from smart_badge_api.api.data_scope import build_permission_scope, staff_scope_condition
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.core.config import PROJECT_ROOT, get_settings
from smart_badge_api.core.permissions import (
    can_manage_role,
    is_global_role,
    normalize_permission_role,
    permission_role_level,
    role_requires_hospital,
)
from smart_badge_api.dingtalk import (
    DingTalkApiError,
    DingTalkConfigError,
    lookup_dingtalk_user_by_job_number,
)
from smart_badge_api.db.models import AuditLog, PositionProfile, Staff, User, VisitOrder, WecomTenant
from smart_badge_api.db.session import get_db
from smart_badge_api.db.system_defaults import ensure_system_management_defaults
from smart_badge_api.schemas.pagination import PaginatedResponse, make_page_response
from smart_badge_api.schemas.staff import (
    StaffAccountActionOut,
    StaffBadgeBindingCandidateOut,
    StaffBadgeBindingUpdate,
    StaffCreate,
    StaffDirectorySyncStatus,
    StaffHospitalOptionOut,
    StaffIdentityLookupOut,
    StaffImportRequest,
    StaffImportResult,
    StaffImportRow,
    StaffOut,
    StaffUpdate,
)
from smart_badge_api.staff_sync import (
    STAFF_DIRECTORY_SYNC_AUDIT_ACTION_NAME,
    STAFF_DIRECTORY_SYNC_AUDIT_MODULE_NAME,
    lookup_staff_directory_records,
    parse_staff_directory_refresh_log_payload,
)

router = APIRouter(prefix="/staff", tags=["人员管理"])
logger = logging.getLogger("smart_badge.staff_routes")
ALL_INSTITUTIONS_LABEL = "所有机构"


def _is_global_permission_role(role: str | None) -> bool:
    return is_global_role(normalize_permission_role(role))


def _staff_scope_fields_for_role(
    permission_role: str | None,
    hospital_code: str | None,
    hospital_short_name: str | None,
) -> tuple[str | None, str | None]:
    if _is_global_permission_role(permission_role):
        return None, ALL_INSTITUTIONS_LABEL
    return hospital_code, hospital_short_name


def _to_out(staff: Staff, *, position_name: str | None) -> StaffOut:
    permission_role = normalize_permission_role(getattr(staff, "permission_role", None))
    hospital_code, hospital_short_name = _staff_scope_fields_for_role(
        permission_role,
        staff.hospital_code,
        staff.hospital_short_name,
    )
    return StaffOut(
        id=staff.id,
        name=staff.name,
        phone=staff.phone,
        external_account=staff.external_account,
        wecom_user_id=staff.wecom_user_id,
        wecom_corp_id=staff.wecom_corp_id,
        gender=staff.gender,
        hospital_code=hospital_code,
        hospital_short_name=hospital_short_name,
        position_id=staff.position_id,
        position_name=position_name,
        role=staff.role,
        permission_role=permission_role,
        badge_id=staff.badge_id,
        is_doctor=staff.is_doctor,
        is_nurse=staff.is_nurse,
        is_anesthetist=staff.is_anesthetist,
        is_cashier=staff.is_cashier,
        is_guide=staff.is_guide,
        is_pre_advisor=staff.is_pre_advisor,
        is_onsite_advisor=staff.is_onsite_advisor,
        is_advisor_assistant=staff.is_advisor_assistant,
        is_doctor_assistant=staff.is_doctor_assistant,
        is_vip_service=staff.is_vip_service,
        is_active=staff.is_active,
    )


def _to_out_with_account(
    staff: Staff,
    *,
    position_name: str | None,
    account_user: User | None,
) -> StaffOut:
    out = _to_out(staff, position_name=position_name)
    last_login_at = getattr(account_user, "last_login_at", None) if account_user else None
    out.account_opened = account_user is not None
    out.account_username = account_user.username if account_user else None
    out.account_is_active = account_user.is_active if account_user else None
    if "account_last_login_at" in getattr(type(out), "model_fields", {}):
        out.account_last_login_at = last_login_at.isoformat() if last_login_at else None
    return out


def _to_account_action_out(
    *,
    staff: Staff,
    user: User,
    message: str,
    created: bool = False,
    source_field: str | None = None,
    source_label: str | None = None,
    temporary_password: str | None = None,
) -> StaffAccountActionOut:
    return StaffAccountActionOut(
        staff_id=staff.id,
        staff_name=staff.name,
        username=user.username,
        is_active=user.is_active,
        created=created,
        source_field=source_field,
        source_label=source_label,
        temporary_password=temporary_password,
        message=message,
    )


def _to_badge_binding_candidate_out(
    staff: Staff,
    *,
    position_name: str | None,
    account_user: User | None,
) -> StaffBadgeBindingCandidateOut:
    hospital_code, hospital_short_name = _staff_scope_fields_for_role(
        staff.permission_role,
        staff.hospital_code,
        staff.hospital_short_name,
    )
    return StaffBadgeBindingCandidateOut(
        id=staff.id,
        name=staff.name,
        external_account=staff.external_account,
        badge_id=staff.badge_id,
        hospital_code=hospital_code,
        hospital_short_name=hospital_short_name,
        position_name=position_name,
        is_active=staff.is_active,
        account_opened=account_user is not None,
        account_username=account_user.username if account_user else None,
        account_is_active=account_user.is_active if account_user else None,
    )


def _coerce_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _coerce_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _clean_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


@lru_cache(maxsize=1)
def _load_dingtalk_user_export_by_job_number() -> dict[str, dict[str, object]]:
    exports_dir = PROJECT_ROOT / "exports"
    if not exports_dir.exists():
        return {}
    files = sorted(
        exports_dir.glob("dingtalk_users_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("failed to read DingTalk user export %s", path, exc_info=True)
            continue
        users = payload.get("users") if isinstance(payload, dict) else None
        if not isinstance(users, list):
            continue
        result: dict[str, dict[str, object]] = {}
        for item in users:
            if not isinstance(item, dict):
                continue
            job_number = _clean_text(item.get("job_number"))
            name = _clean_text(item.get("name"))
            if job_number and name:
                result.setdefault(job_number, item)
        return result
    return {}


async def _lookup_staff_identity_from_directory(external_account: str) -> StaffIdentityLookupOut | None:
    try:
        records = await asyncio.to_thread(lookup_staff_directory_records, [external_account])
    except Exception:
        logger.warning("failed to lookup staff directory for %s", external_account, exc_info=True)
        return None
    record = records.get(external_account)
    if record is None or not record.name:
        return None
    return StaffIdentityLookupOut(
        external_account=external_account,
        name=record.name,
        hospital_code=record.hospital_code,
        hospital_short_name=record.hospital_short_name,
        source="staff_directory",
    )


async def _lookup_staff_identity_from_visit_orders(
    db: AsyncSession,
    external_account: str,
    *,
    hospital_code: str | None = None,
) -> StaffIdentityLookupOut | None:
    conditions = [
        VisitOrder.fzuer == external_account,
        VisitOrder.fzr_id_dq == external_account,
        VisitOrder.d_fzuer == external_account,
        VisitOrder.advxc == external_account,
        VisitOrder.advyq == external_account,
    ]
    stmt = (
        select(VisitOrder)
        .where(or_(*conditions))
        .order_by(VisitOrder.sjrq.desc(), VisitOrder.crtdt.desc(), VisitOrder.crttm.desc())
        .limit(20)
    )
    if hospital_code:
        stmt = stmt.where(VisitOrder.jgbm == hospital_code)
    rows = (await db.execute(stmt)).scalars().all()
    for order in rows:
        if external_account in {_clean_text(order.fzuer), _clean_text(order.fzr_id_dq), _clean_text(order.d_fzuer)}:
            name = _clean_text(order.fzr_name_dq) or _clean_text(order.fzuer_long)
            if name:
                return StaffIdentityLookupOut(
                    external_account=external_account,
                    name=name,
                    hospital_code=_clean_text(order.jgbm),
                    source="visit_order",
                )
        if external_account == _clean_text(order.advxc):
            name = _clean_text(order.advxc_long)
            if name:
                return StaffIdentityLookupOut(
                    external_account=external_account,
                    name=name,
                    hospital_code=_clean_text(order.jgbm),
                    source="visit_order",
                )
        if external_account == _clean_text(order.advyq):
            name = _clean_text(order.advyq_name)
            if name:
                return StaffIdentityLookupOut(
                    external_account=external_account,
                    name=name,
                    hospital_code=_clean_text(order.jgbm),
                    source="visit_order",
                )
    return None


def _lookup_staff_identity_from_dingtalk_export(external_account: str) -> StaffIdentityLookupOut | None:
    item = _load_dingtalk_user_export_by_job_number().get(external_account)
    if not item:
        return None
    name = _clean_text(item.get("name"))
    if not name:
        return None
    return StaffIdentityLookupOut(
        external_account=external_account,
        name=name,
        phone=_clean_text(item.get("mobile")),
        dingtalk_user_id=_clean_text(item.get("userid")) or _clean_text(item.get("user_id")),
        source="dingtalk_export",
    )


async def _lookup_staff_identity_from_dingtalk_api(external_account: str) -> StaffIdentityLookupOut | None:
    try:
        item = await lookup_dingtalk_user_by_job_number(external_account)
    except (DingTalkApiError, DingTalkConfigError):
        logger.warning("failed to lookup DingTalk contact for %s", external_account, exc_info=True)
        return None
    except Exception:
        logger.warning("unexpected DingTalk contact lookup error for %s", external_account, exc_info=True)
        return None
    if not item:
        return None
    name = _clean_text(item.get("name"))
    if not name:
        return None
    return StaffIdentityLookupOut(
        external_account=external_account,
        name=name,
        phone=_clean_text(item.get("mobile")),
        dingtalk_user_id=_clean_text(item.get("userid")) or _clean_text(item.get("user_id")),
        source="dingtalk_api",
    )


def _with_dingtalk_user_id(
    identity: StaffIdentityLookupOut,
    dingtalk_identity: StaffIdentityLookupOut | None,
) -> StaffIdentityLookupOut:
    if identity.dingtalk_user_id or not dingtalk_identity or not dingtalk_identity.dingtalk_user_id:
        return identity
    return identity.model_copy(update={"dingtalk_user_id": dingtalk_identity.dingtalk_user_id})


async def _lookup_staff_identity(
    db: AsyncSession,
    external_account: str,
    *,
    hospital_code: str | None = None,
) -> StaffIdentityLookupOut | None:
    code = _clean_text(external_account)
    if not code:
        return None
    dingtalk_identity = await _lookup_staff_identity_from_dingtalk_api(code)
    dingtalk_export_identity = None
    if not dingtalk_identity or not dingtalk_identity.dingtalk_user_id:
        dingtalk_export_identity = _lookup_staff_identity_from_dingtalk_export(code)
    if dingtalk_identity:
        return _with_dingtalk_user_id(dingtalk_identity, dingtalk_export_identity)
    directory_identity = await _lookup_staff_identity_from_directory(code)
    if directory_identity:
        return _with_dingtalk_user_id(directory_identity, dingtalk_export_identity)
    visit_order_identity = await _lookup_staff_identity_from_visit_orders(
        db,
        code,
        hospital_code=hospital_code,
    )
    if visit_order_identity:
        return _with_dingtalk_user_id(visit_order_identity, dingtalk_export_identity)
    return dingtalk_export_identity


async def _load_institution_identity_map(db: AsyncSession, hospital_codes: set[str]) -> dict[str, dict[str, str | None]]:
    normalized_codes = {code.strip() for code in hospital_codes if code and code.strip()}
    if not normalized_codes:
        return {}
    rows = (
        await db.execute(
            select(WecomTenant.default_hospital_code, WecomTenant.name, WecomTenant.corp_id)
            .where(
                WecomTenant.default_hospital_code.in_(normalized_codes),
                WecomTenant.default_hospital_code.is_not(None),
                WecomTenant.default_hospital_code != "",
            )
            .order_by(WecomTenant.is_active.desc(), WecomTenant.updated_at.desc())
        )
    ).all()
    identity_map: dict[str, dict[str, str | None]] = {}
    for hospital_code, hospital_name, corp_id in rows:
        code = _clean_text(hospital_code)
        name = _clean_text(hospital_name)
        if code and name and code not in identity_map:
            identity_map[code] = {
                "name": name,
                "corp_id": _clean_text(corp_id),
            }
    return identity_map


async def _resolve_institution_identity(db: AsyncSession, hospital_code: str | None) -> tuple[str | None, str | None]:
    normalized_code = _clean_text(hospital_code)
    if not normalized_code:
        return None, None
    identity_map = await _load_institution_identity_map(db, {normalized_code})
    identity = identity_map.get(normalized_code)
    if identity is None:
        raise HTTPException(400, "机构编码不存在，请先在机构管理中配置")
    return identity["name"], identity["corp_id"]


async def _resolve_position_role(db: AsyncSession, position_id: str | None, fallback_role: str | None) -> str:
    if position_id:
        position = await db.get(PositionProfile, position_id)
        if not position:
            raise HTTPException(400, "Position not found")
        return normalize_permission_role(position.mapped_role)
    return normalize_permission_role(fallback_role)


async def _validate_position(db: AsyncSession, *, position_id: str | None) -> None:
    if position_id and not await db.get(PositionProfile, position_id):
        raise HTTPException(400, "Position not found")


async def _ensure_super_admin_uniqueness(
    db: AsyncSession,
    *,
    permission_role: str,
    exclude_staff_id: str | None = None,
) -> None:
    if normalize_permission_role(permission_role) != "super_admin":
        return
    stmt = select(Staff.id).where(Staff.permission_role == "super_admin")
    if exclude_staff_id:
        stmt = stmt.where(Staff.id != exclude_staff_id)
    existing = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(400, "系统中只能存在一位超级管理员")


def _assert_manage_permission(
    current_user: User,
    *,
    target_current_role: str | None,
    target_next_role: str,
    target_staff_id: str | None,
) -> None:
    current_role = normalize_permission_role(current_user.role)
    current_staff_id = current_user.staff_id
    is_self = bool(current_staff_id and current_staff_id == target_staff_id)
    normalized_current_target = normalize_permission_role(target_current_role)
    normalized_next_target = normalize_permission_role(target_next_role)

    if is_self:
        if normalized_next_target != current_role:
            raise HTTPException(403, "不能修改自己的权限角色")
        return

    if permission_role_level(current_role) <= permission_role_level(normalized_current_target):
        raise HTTPException(403, "无权管理同级或更高权限人员")
    if not can_manage_role(current_role, normalized_next_target):
        raise HTTPException(403, "无权分配该权限角色")


def _assert_create_permission(current_user: User, *, target_next_role: str) -> None:
    current_role = normalize_permission_role(current_user.role)
    normalized_next_target = normalize_permission_role(target_next_role)

    if current_role == "hospital_admin" and normalized_next_target in {"staff", "hospital_admin"}:
        return

    _assert_manage_permission(
        current_user,
        target_current_role=None,
        target_next_role=normalized_next_target,
        target_staff_id=None,
    )


def _assert_scope_assignment(
    current_user: User,
    *,
    permission_role: str,
    hospital_code: str | None,
) -> None:
    current_role = normalize_permission_role(current_user.role)
    if role_requires_hospital(permission_role) and not hospital_code:
        raise HTTPException(400, "机构管理员必须绑定机构编码")

    if current_role == "hospital_admin":
        if hospital_code != current_user.hospital_code:
            raise HTTPException(403, "机构管理员只能分配本机构范围的数据权限")


async def _sync_linked_user_scope(db: AsyncSession, staff: Staff) -> None:
    linked_user = await get_linked_user_by_staff_id(db, staff.id)
    if linked_user is None:
        return
    sync_user_scope_from_staff(linked_user, staff)


async def _load_account_user_map(db: AsyncSession, staff_ids: list[str]) -> dict[str, User]:
    if not staff_ids:
        return {}
    users = (
        await db.execute(select(User).where(User.staff_id.in_(staff_ids)).order_by(User.created_at.desc()))
    ).scalars().all()
    user_map: dict[str, User] = {}
    for user in users:
        if user.staff_id and user.staff_id not in user_map:
            user_map[user.staff_id] = user
    return user_map


async def _get_scoped_staff_or_404(db: AsyncSession, staff_id: str, current_user: User) -> Staff:
    scope = await build_permission_scope(current_user)
    staff = (
        await db.execute(select(Staff).where(Staff.id == staff_id, staff_scope_condition(scope)).limit(1))
    ).scalar_one_or_none()
    if staff is None:
        raise HTTPException(404, "Staff not found")
    return staff


def _assert_account_manage_allowed(current_user: User, target_staff: Staff) -> None:
    if current_user.staff_id and current_user.staff_id == target_staff.id:
        raise HTTPException(403, "请在个人中心管理自己的登录账号")
    _assert_manage_permission(
        current_user,
        target_current_role=target_staff.permission_role,
        target_next_role=target_staff.permission_role,
        target_staff_id=target_staff.id,
    )


async def bulk_import_staff_rows(db: AsyncSession, rows: list[StaffImportRow]) -> list[Staff]:
    if not rows:
        raise HTTPException(400, "没有可导入的人员数据")

    positions = {
        item.name: item
        for item in (await db.execute(select(PositionProfile))).scalars().all()
    }
    hospital_codes = {_clean_text(row.hospital_code) for row in rows}
    hospital_identity_map = await _load_institution_identity_map(db, {code for code in hospital_codes if code})

    pending_staff: list[Staff] = []
    for index, row in enumerate(rows, start=1):
        name = row.name.strip()
        if not name:
            raise HTTPException(400, f"第 {index} 行：姓名不能为空")

        position = None
        if row.position_name:
            position = positions.get(row.position_name.strip())
            if position is None:
                raise HTTPException(400, f"第 {index} 行：岗位不存在 - {row.position_name}")

        hospital_code = _clean_text(row.hospital_code)
        hospital_identity = hospital_identity_map.get(hospital_code or "")
        hospital_short_name = (
            (hospital_identity or {}).get("name")
            if hospital_code
            else None
        ) or _clean_text(row.hospital_short_name)
        permission_role = normalize_permission_role(row.permission_role or (position.mapped_role if position else None))
        wecom_corp_id = (hospital_identity or {}).get("corp_id") or row.wecom_corp_id or None
        if _is_global_permission_role(permission_role):
            hospital_code = None
            hospital_short_name = ALL_INSTITUTIONS_LABEL
            hospital_identity = None
            wecom_corp_id = None

        staff = Staff(
            name=name,
            phone=row.phone or None,
            external_account=row.external_account or None,
            wecom_user_id=row.wecom_user_id or None,
            wecom_corp_id=wecom_corp_id,
            gender=row.gender or None,
            hospital_code=hospital_code,
            hospital_short_name=hospital_short_name,
            position_id=position.id if position else None,
            role="consultant",
            permission_role=permission_role,
            badge_id=None,
            is_active=row.is_active,
        )
        pending_staff.append(staff)

    db.add_all(pending_staff)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(400, "批量导入失败，请检查数据是否重复或格式不正确") from exc

    for item in pending_staff:
        await db.refresh(item)

    return pending_staff


@router.get("", response_model=PaginatedResponse[StaffOut])
async def list_staff(
    keyword: str | None = Query(None),
    position_id: str | None = Query(None),
    badge_id: str | None = Query(None),
    hospital_code: str | None = Query(None),
    account_status: Literal["not_opened", "active", "disabled"] | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await ensure_system_management_defaults(db)
    keyword = keyword if isinstance(keyword, str) else None
    position_id = position_id if isinstance(position_id, str) else None
    badge_id = badge_id if isinstance(badge_id, str) else None
    hospital_code = hospital_code if isinstance(hospital_code, str) else None
    page = page if isinstance(page, int) else 1
    page_size = page_size if isinstance(page_size, int) else 20
    if account_status not in {"not_opened", "active", "disabled"}:
        account_status = None
    scope = await build_permission_scope(current_user)
    linked_user_sq = (
        select(
            User.id.label("user_id"),
            User.staff_id.label("staff_id"),
            User.is_active.label("is_active"),
            func.row_number()
            .over(
                partition_by=User.staff_id,
                order_by=(User.updated_at.desc(), User.created_at.desc(), User.id.desc()),
            )
            .label("row_num"),
        )
        .where(User.staff_id.is_not(None))
        .subquery()
    )

    stmt = (
        select(
            Staff,
            PositionProfile.name.label("position_name"),
        )
        .outerjoin(PositionProfile, PositionProfile.id == Staff.position_id)
        .outerjoin(
            linked_user_sq,
            and_(linked_user_sq.c.staff_id == Staff.id, linked_user_sq.c.row_num == 1),
        )
        .where(staff_scope_condition(scope))
        .order_by(Staff.created_at.desc())
    )

    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                Staff.name.ilike(like),
                Staff.phone.ilike(like),
                Staff.external_account.ilike(like),
                Staff.wecom_user_id.ilike(like),
                Staff.wecom_corp_id.ilike(like),
            )
        )
    if position_id:
        stmt = stmt.where(Staff.position_id == position_id)
    if hospital_code and hospital_code.strip():
        stmt = stmt.where(Staff.hospital_code == hospital_code.strip())
    if badge_id:
        stmt = stmt.where(Staff.badge_id.ilike(f"%{badge_id.strip()}%"))
    if account_status == "not_opened":
        stmt = stmt.where(linked_user_sq.c.user_id.is_(None))
    elif account_status == "active":
        stmt = stmt.where(
            linked_user_sq.c.user_id.is_not(None),
            linked_user_sq.c.is_active.is_(True),
        )
    elif account_status == "disabled":
        stmt = stmt.where(
            linked_user_sq.c.user_id.is_not(None),
            linked_user_sq.c.is_active.is_(False),
        )

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))).all()
    staff_ids = [staff.id for staff, _position_name in rows]
    account_user_map = await _load_account_user_map(db, staff_ids)
    items = [
        _to_out_with_account(
            staff,
            position_name=position_name,
            account_user=account_user_map.get(staff.id),
        )
        for staff, position_name in rows
    ]
    return make_page_response(items, total, page, page_size)


@router.get("/hospital-options", response_model=list[StaffHospitalOptionOut])
async def list_staff_hospital_options(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    scope = await build_permission_scope(current_user)
    tenant_stmt = (
        select(WecomTenant.default_hospital_code, WecomTenant.name)
        .where(
            WecomTenant.default_hospital_code.is_not(None),
            WecomTenant.default_hospital_code != "",
        )
        .order_by(WecomTenant.default_hospital_code.asc(), WecomTenant.is_active.desc(), WecomTenant.updated_at.desc())
    )
    if not is_global_role(scope.role):
        if not scope.hospital_code:
            return []
        tenant_stmt = tenant_stmt.where(WecomTenant.default_hospital_code == scope.hospital_code)

    tenant_rows = (await db.execute(tenant_stmt)).all()
    options: dict[str, str] = {}
    for hospital_code, hospital_name in tenant_rows:
        code = _clean_text(hospital_code)
        name = _clean_text(hospital_name)
        if code and name and code not in options:
            options[code] = name

    if options:
        return [
            StaffHospitalOptionOut(hospital_code=code, hospital_name=name)
            for code, name in sorted(options.items())
        ]

    rows = (
        await db.execute(
            select(
                Staff.hospital_code,
                func.max(Staff.hospital_short_name).label("hospital_name"),
            )
            .where(
                staff_scope_condition(scope),
                Staff.hospital_code.is_not(None),
                Staff.hospital_code != "",
            )
            .group_by(Staff.hospital_code)
            .order_by(Staff.hospital_code.asc())
        )
    ).all()
    return [
        StaffHospitalOptionOut(
            hospital_code=str(hospital_code).strip(),
            hospital_name=str(hospital_name or hospital_code).strip(),
        )
        for hospital_code, hospital_name in rows
        if str(hospital_code or "").strip()
    ]


@router.get("/identity-lookup", response_model=StaffIdentityLookupOut)
async def lookup_staff_identity(
    external_account: str = Query(..., min_length=1),
    hospital_code: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    code = _clean_text(external_account)
    if not code:
        raise HTTPException(400, "员工编号不能为空")
    normalized_hospital_code = _clean_text(hospital_code)
    scope = await build_permission_scope(current_user)
    if scope.role == "hospital_admin":
        normalized_hospital_code = scope.hospital_code
    identity = await _lookup_staff_identity(db, code, hospital_code=normalized_hospital_code)
    if not identity or not identity.name:
        raise HTTPException(404, "未根据员工编号找到员工姓名，请手动填写姓名")
    return identity


@router.post("", response_model=StaffOut, status_code=201)
async def create_staff(
    body: StaffCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _validate_position(db, position_id=body.position_id)
    permission_role = await _resolve_position_role(db, body.position_id, body.permission_role)
    hospital_code = _clean_text(body.hospital_code)
    external_account = _clean_text(body.external_account)
    if not external_account:
        raise HTTPException(400, "员工编号不能为空")
    identity = (
        await _lookup_staff_identity(db, external_account, hospital_code=hospital_code)
        if not _clean_text(body.name) or not hospital_code
        else None
    )
    resolved_name = _clean_text(body.name) or _clean_text(identity.name if identity else None)
    if not resolved_name:
        raise HTTPException(400, "未根据员工编号自动获取姓名，请手动填写姓名")
    if not hospital_code and identity and identity.hospital_code:
        hospital_code = _clean_text(identity.hospital_code)
    if _is_global_permission_role(permission_role):
        hospital_code = None
    _assert_create_permission(current_user, target_next_role=permission_role)
    _assert_scope_assignment(
        current_user,
        permission_role=permission_role,
        hospital_code=hospital_code,
    )
    await _ensure_super_admin_uniqueness(db, permission_role=permission_role)
    hospital_name, wecom_corp_id = await _resolve_institution_identity(db, hospital_code)
    if _is_global_permission_role(permission_role):
        hospital_name = ALL_INSTITUTIONS_LABEL
        wecom_corp_id = None
    payload = body.model_dump(exclude={"role", "permission_role", "hospital_short_name", "wecom_corp_id"})
    payload["name"] = resolved_name
    payload["hospital_code"] = hospital_code
    payload["hospital_short_name"] = hospital_name
    payload["wecom_corp_id"] = wecom_corp_id
    payload["external_account"] = external_account
    if identity and identity.phone and not payload.get("phone"):
        payload["phone"] = identity.phone
    person = Staff(
        **payload,
        role=body.role or "consultant",
        permission_role=permission_role,
        badge_id=None,
    )
    db.add(person)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(400, "人员保存失败，员工编号或企业微信 UserId 可能重复") from exc
    await db.refresh(person)

    position = await db.get(PositionProfile, person.position_id) if person.position_id else None
    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="人员管理",
        action_name="新增人员",
        content=f"新增人员：{person.name} 手机号：{person.phone or '-'} 岗位：{position.name if position else '-'} 权限：{person.permission_role}",
    )
    return _to_out(person, position_name=position.name if position else None)


@router.post("/import", response_model=StaffImportResult, status_code=201)
async def import_staff(
    body: StaffImportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    imported = await bulk_import_staff_rows(db, body.rows)
    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="人员管理",
        action_name="批量导入人员",
        content=f"批量导入人员：{len(imported)} 条",
    )
    return StaffImportResult(created_count=len(imported))


@router.get("/sync-status", response_model=StaffDirectorySyncStatus)
async def get_staff_directory_sync_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    scheduler_enabled = bool(getattr(request.app.state, "staff_sync_scheduler_configured", False))
    scheduler_started_at = getattr(request.app.state, "staff_sync_scheduler_started_at", None)
    scheduler_note = getattr(request.app.state, "staff_sync_scheduler_note", None)
    scheduler_task = getattr(request.app.state, "staff_sync_task", None)
    scheduler_running = bool(scheduler_task and not scheduler_task.done())

    if scheduler_task and scheduler_task.done() and not scheduler_task.cancelled():
        task_error = scheduler_task.exception()
        if task_error is not None:
            scheduler_note = f"员工状态定时同步服务已异常退出：{type(task_error).__name__}: {task_error}"

    latest_log = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.module_name == STAFF_DIRECTORY_SYNC_AUDIT_MODULE_NAME,
                AuditLog.action_name == STAFF_DIRECTORY_SYNC_AUDIT_ACTION_NAME,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    last_sync_status = "not_started"
    last_synced_at = None
    next_scheduled_at = None
    checked_count = None
    updated_count = None
    missing_count = None
    deactivated_count = None
    error_message = None

    if latest_log:
        payload = parse_staff_directory_refresh_log_payload(latest_log.content)
        status = payload.get("status")
        if status in {"success", "failed"}:
            last_sync_status = status
        last_synced_at = latest_log.created_at
        if settings.staff_refresh_interval_seconds > 0:
            next_scheduled_at = latest_log.created_at + timedelta(seconds=settings.staff_refresh_interval_seconds)
        checked_count = _coerce_int(payload.get("checked_count"))
        updated_count = _coerce_int(payload.get("updated_count"))
        missing_count = _coerce_int(payload.get("missing_count"))
        deactivated_count = _coerce_int(payload.get("deactivated_count"))
        error_message = _coerce_str(payload.get("error_message"))

    if scheduler_enabled and not scheduler_running and not scheduler_note:
        scheduler_note = "员工状态定时同步服务未在运行，请检查后端日志"
    if scheduler_enabled and not last_synced_at and not scheduler_note:
        scheduler_note = "员工状态定时同步服务已启动，等待首次执行"
    if not scheduler_enabled and not scheduler_note:
        scheduler_note = "员工状态定时同步服务未启用"

    return StaffDirectorySyncStatus(
        scheduler_enabled=scheduler_enabled,
        scheduler_running=scheduler_running,
        scheduler_started_at=scheduler_started_at,
        scheduler_note=scheduler_note,
        interval_seconds=settings.staff_refresh_interval_seconds,
        last_synced_at=last_synced_at,
        next_scheduled_at=next_scheduled_at,
        last_sync_status=last_sync_status,
        checked_count=checked_count,
        updated_count=updated_count,
        missing_count=missing_count,
        deactivated_count=deactivated_count,
        error_message=error_message,
    )


@router.get("/badge-binding-candidates", response_model=list[StaffBadgeBindingCandidateOut])
async def list_staff_badge_binding_candidates(
    keyword: str | None = Query(None),
    hospital_code: str | None = Query(None),
    include_inactive: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await ensure_system_management_defaults(db)
    keyword = keyword if isinstance(keyword, str) else None
    scope = await build_permission_scope(current_user)
    stmt = (
        select(
            Staff,
            PositionProfile.name.label("position_name"),
        )
        .outerjoin(PositionProfile, PositionProfile.id == Staff.position_id)
        .where(
            staff_scope_condition(scope),
        )
        .order_by(Staff.is_active.desc(), Staff.name.asc(), Staff.created_at.desc())
    )

    if not include_inactive:
        stmt = stmt.where(Staff.is_active.is_(True))

    normalized_hospital_code = _clean_text(hospital_code)
    if normalized_hospital_code:
        stmt = stmt.where(Staff.hospital_code == normalized_hospital_code)

    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                Staff.name.ilike(like),
                Staff.external_account.ilike(like),
                Staff.badge_id.ilike(like),
                Staff.hospital_short_name.ilike(like),
            )
        )

    rows = (await db.execute(stmt)).all()
    staff_ids = [staff.id for staff, _position_name in rows]
    account_user_map = await _load_account_user_map(db, staff_ids)
    return [
        _to_badge_binding_candidate_out(
            staff,
            position_name=position_name,
            account_user=account_user_map.get(staff.id),
        )
        for staff, position_name in rows
    ]


@router.put("/{staff_id}/badge-binding", response_model=StaffOut)
async def update_staff_badge_binding(
    staff_id: str,
    body: StaffBadgeBindingUpdate,
    current_user: User = Depends(get_current_user),
):
    _ = (staff_id, body, current_user)
    raise HTTPException(status_code=410, detail="人员与工牌绑定请在“朗姿工牌”页面操作")


@router.post("/{staff_id}/account/enable", response_model=StaffAccountActionOut)
async def enable_staff_account(
    staff_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    staff = await _get_scoped_staff_or_404(db, staff_id, current_user)
    _assert_account_manage_allowed(current_user, staff)
    if not staff.is_active:
        raise HTTPException(400, "人员已禁用，不能开通登录账号")

    try:
        provisioned = await provision_staff_account(db, staff=staff)
    except AccountProvisioningError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc

    if provisioned.created:
        action_name = "开通账号"
        message = "账号已开通"
    elif provisioned.activated_existing:
        action_name = "启用账号"
        message = "账号已启用"
    else:
        action_name = "查看账号状态"
        message = "账号已开通，可直接登录"
    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="账号管理",
        action_name=action_name,
        content=(
            f"{action_name}：{staff.name}，登录账号 {provisioned.user.username}"
            + (
                f"（{provisioned.identifier.source_label}）"
                if provisioned.identifier and provisioned.identifier.source_label
                else ""
            )
        ),
    )
    return _to_account_action_out(
        staff=staff,
        user=provisioned.user,
        message=message,
        created=provisioned.created,
        source_field=provisioned.identifier.source_field if provisioned.identifier else None,
        source_label=provisioned.identifier.source_label if provisioned.identifier else None,
        temporary_password=provisioned.temporary_password,
    )


@router.post("/{staff_id}/account/reset-password", response_model=StaffAccountActionOut)
async def reset_staff_account(
    staff_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    staff = await _get_scoped_staff_or_404(db, staff_id, current_user)
    _assert_account_manage_allowed(current_user, staff)
    try:
        provisioned = await reset_staff_account_password(db, staff=staff)
    except AccountProvisioningError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc

    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="账号管理",
        action_name="重置密码",
        content=f"重置密码：{staff.name}，登录账号 {provisioned.user.username}",
    )
    return _to_account_action_out(
        staff=staff,
        user=provisioned.user,
        message="密码已重置为默认密码",
        temporary_password=provisioned.temporary_password,
    )


@router.post("/{staff_id}/account/disable", response_model=StaffAccountActionOut)
async def disable_staff_account(
    staff_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    staff = await _get_scoped_staff_or_404(db, staff_id, current_user)
    _assert_account_manage_allowed(current_user, staff)
    try:
        user = await set_staff_account_active(db, staff=staff, is_active=False)
    except AccountProvisioningError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc

    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="账号管理",
        action_name="停用账号",
        content=f"停用账号：{staff.name}，登录账号 {user.username}",
    )
    return _to_account_action_out(
        staff=staff,
        user=user,
        message="账号已停用",
    )


@router.post("/{staff_id}/account/activate", response_model=StaffAccountActionOut)
async def activate_staff_account(
    staff_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    staff = await _get_scoped_staff_or_404(db, staff_id, current_user)
    _assert_account_manage_allowed(current_user, staff)
    try:
        user = await set_staff_account_active(db, staff=staff, is_active=True)
    except AccountProvisioningError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc

    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="账号管理",
        action_name="启用账号",
        content=f"启用账号：{staff.name}，登录账号 {user.username}",
    )
    return _to_account_action_out(
        staff=staff,
        user=user,
        message="账号已启用",
    )


@router.get("/{staff_id}", response_model=StaffOut)
async def get_staff_detail(
    staff_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await ensure_system_management_defaults(db)
    staff = await _get_scoped_staff_or_404(db, staff_id, current_user)
    position = await db.get(PositionProfile, staff.position_id) if staff.position_id else None
    linked_user = await get_linked_user_by_staff_id(db, staff.id)
    return _to_out_with_account(
        staff,
        position_name=position.name if position else None,
        account_user=linked_user,
    )


@router.put("/{staff_id}", response_model=StaffOut)
async def update_staff(
    staff_id: str,
    body: StaffUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    person = await db.get(Staff, staff_id)
    if not person:
        raise HTTPException(404, "Staff not found")

    updates = body.model_dump(exclude_unset=True)
    await _validate_position(db, position_id=updates.get("position_id", person.position_id))
    next_permission_role = await _resolve_position_role(
        db,
        updates.get("position_id", person.position_id),
        updates.get("permission_role", person.permission_role),
    )
    next_hospital_code = _clean_text(updates.get("hospital_code", person.hospital_code))
    if _is_global_permission_role(next_permission_role):
        next_hospital_code = None
    next_hospital_name, next_wecom_corp_id = await _resolve_institution_identity(db, next_hospital_code)
    if _is_global_permission_role(next_permission_role):
        next_hospital_name = ALL_INSTITUTIONS_LABEL
        next_wecom_corp_id = None
    _assert_manage_permission(
        current_user,
        target_current_role=person.permission_role,
        target_next_role=next_permission_role,
        target_staff_id=person.id,
    )
    _assert_scope_assignment(
        current_user,
        permission_role=next_permission_role,
        hospital_code=next_hospital_code,
    )
    await _ensure_super_admin_uniqueness(db, permission_role=next_permission_role, exclude_staff_id=person.id)
    for key, value in updates.items():
        if key in {"permission_role", "hospital_code", "hospital_short_name", "wecom_corp_id"}:
            continue
        setattr(person, key, value)
    if "role" in updates:
        person.role = updates["role"] or person.role
    person.permission_role = next_permission_role
    person.hospital_code = next_hospital_code
    person.hospital_short_name = next_hospital_name
    person.wecom_corp_id = next_wecom_corp_id
    await _sync_linked_user_scope(db, person)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(400, "人员更新失败，员工编号或企业微信 UserId 可能重复") from exc
    await db.refresh(person)
    # If a linked user account exists for this staff, its cached scope/role
    # may have changed — invalidate so all workers reload from DB.
    _linked_user_after = await get_linked_user_by_staff_id(db, person.id)
    if _linked_user_after is not None:
        from smart_badge_api.api.deps import invalidate_user_cache
        await invalidate_user_cache(_linked_user_after.id)

    position = await db.get(PositionProfile, person.position_id) if person.position_id else None
    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="人员管理",
        action_name="更新人员",
        content=f"更新人员：{person.name} 岗位：{position.name if position else '-'} 权限：{person.permission_role}",
    )
    return _to_out(person, position_name=position.name if position else None)


@router.delete("/{staff_id}", status_code=204)
async def delete_staff(
    staff_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    person = await _get_scoped_staff_or_404(db, staff_id, current_user)
    _assert_manage_permission(
        current_user,
        target_current_role=person.permission_role,
        target_next_role=person.permission_role,
        target_staff_id=person.id,
    )
    name = person.name
    linked_user = await get_linked_user_by_staff_id(db, person.id)
    linked_user_id = linked_user.id if linked_user is not None else None
    if linked_user is not None:
        linked_user.is_active = False
        linked_user.staff_id = None
    await db.delete(person)
    await db.commit()
    if linked_user_id:
        from smart_badge_api.api.deps import invalidate_user_cache
        await invalidate_user_cache(linked_user_id)
    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="人员管理",
        action_name="删除人员",
        content=(
            f"删除人员：{name}"
            + (f"，并停用账号 {linked_user.username}" if linked_user is not None else "")
        ),
    )
