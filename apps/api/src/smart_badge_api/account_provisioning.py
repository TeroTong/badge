from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.core.permissions import is_global_role, normalize_permission_role, permission_role_level
from smart_badge_api.core.security import hash_password
from smart_badge_api.db.models import Staff, User

LEGACY_WECOM_USERNAME_PREFIX = "wecom_"


class AccountProvisioningError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(slots=True)
class StaffAccountIdentifier:
    username: str
    source_field: str
    source_label: str


@dataclass(slots=True)
class StaffAccountProvisionResult:
    user: User
    created: bool
    activated_existing: bool
    temporary_password: str | None
    identifier: StaffAccountIdentifier | None


def _first_non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _is_probable_phone(value: str | None) -> bool:
    return bool(value) and value.isdigit() and len(value) == 11 and value.startswith("1")


def _resolve_legacy_phone_identifier(staff: Staff, user: User) -> StaffAccountIdentifier | None:
    legacy_username = _first_non_empty(user.username)
    if not legacy_username or not legacy_username.startswith(LEGACY_WECOM_USERNAME_PREFIX):
        return None

    legacy_suffix = legacy_username[len(LEGACY_WECOM_USERNAME_PREFIX) :]
    phone = _first_non_empty(staff.phone)
    if phone and legacy_suffix == phone:
        return StaffAccountIdentifier(
            username=phone,
            source_field="phone",
            source_label="手机号",
        )

    employee_code = _first_non_empty(staff.external_account)
    wecom_user_id = _first_non_empty(getattr(staff, "wecom_user_id", None))
    if employee_code is None and wecom_user_id == legacy_suffix and _is_probable_phone(legacy_suffix):
        return StaffAccountIdentifier(
            username=legacy_suffix,
            source_field="phone",
            source_label="手机号",
        )

    return None


def sync_user_scope_from_staff(
    user: User,
    staff: Staff,
    *,
    preserve_higher_role: bool = False,
) -> None:
    next_role = normalize_permission_role(getattr(staff, "permission_role", None) or staff.role)
    current_role = normalize_permission_role(getattr(user, "role", None))
    if preserve_higher_role and permission_role_level(current_role) > permission_role_level(next_role):
        user.role = current_role
    else:
        user.role = next_role
    if is_global_role(user.role):
        user.hospital_code = None
        user.hospital_name = "所有机构"
    else:
        user.hospital_code = staff.hospital_code
        user.hospital_name = staff.hospital_short_name


def resolve_staff_account_identifier(staff: Staff) -> StaffAccountIdentifier:
    employee_code = _first_non_empty(staff.external_account)
    if employee_code:
        return StaffAccountIdentifier(
            username=employee_code,
            source_field="external_account",
            source_label="员工编号",
        )

    phone = _first_non_empty(staff.phone)
    if phone:
        return StaffAccountIdentifier(
            username=phone,
            source_field="phone",
            source_label="手机号",
        )

    raise AccountProvisioningError("请先补员工工号或手机号", status_code=400)


def build_default_password(username: str) -> str:
    suffix = username[-4:] if len(username) > 4 else username
    return f"{suffix}@Abcd"


async def _find_unique_active_staff_by_field(
    db: AsyncSession,
    *,
    field_name: str,
    value: str | None,
) -> Staff | None:
    normalized = _first_non_empty(value)
    if normalized is None:
        return None

    field = getattr(Staff, field_name)
    rows = (
        await db.execute(
            select(Staff)
            .where(field == normalized, Staff.is_active.is_(True))
            .order_by(Staff.updated_at.desc(), Staff.created_at.desc(), Staff.id.desc())
            .limit(2)
        )
    ).scalars().all()
    if len(rows) != 1:
        return None
    return rows[0]


async def resolve_staff_for_user(
    db: AsyncSession,
    *,
    user: User,
    persist_link: bool = False,
) -> Staff | None:
    if user.staff_id:
        staff = await db.get(Staff, user.staff_id)
        if staff is not None:
            return staff

    matched_staff = await _find_unique_active_staff_by_field(
        db,
        field_name="wecom_user_id",
        value=user.username,
    )
    if matched_staff is None:
        matched_staff = await _find_unique_active_staff_by_field(
            db,
            field_name="external_account",
            value=user.username,
        )
    if matched_staff is None:
        matched_staff = await _find_unique_active_staff_by_field(
            db,
            field_name="phone",
            value=user.username,
        )
    if matched_staff is None:
        matched_staff = await _find_unique_active_staff_by_field(
            db,
            field_name="name",
            value=user.display_name,
        )

    if matched_staff is None:
        return None

    if persist_link and user.staff_id != matched_staff.id:
        user.staff_id = matched_staff.id
        await db.commit()
        await db.refresh(user)

    return matched_staff


async def get_linked_user_by_staff_id(db: AsyncSession, staff_id: str) -> User | None:
    return (
        await db.execute(
            select(User)
            .where(User.staff_id == staff_id)
            .order_by(User.updated_at.desc(), User.created_at.desc(), User.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _ensure_identifier_available(
    db: AsyncSession,
    *,
    identifier: StaffAccountIdentifier,
    staff_id: str,
) -> None:
    existing = (await db.execute(select(User).where(User.username == identifier.username).limit(1))).scalar_one_or_none()
    if existing is None or existing.staff_id == staff_id:
        return
    raise AccountProvisioningError(
        f"{identifier.source_label} {identifier.username} 对应的登录账号已被其他员工占用，请检查员工工号或手机号是否重复",
        status_code=409,
    )


async def provision_staff_account(
    db: AsyncSession,
    *,
    staff: Staff,
    activate_existing: bool = True,
    preserve_higher_role: bool = False,
) -> StaffAccountProvisionResult:
    user = await get_linked_user_by_staff_id(db, staff.id)
    if user is not None:
        changed = False
        activated_existing = False
        normalized_identifier = _resolve_legacy_phone_identifier(staff, user)
        before_scope = (
            user.role,
            user.hospital_code,
            user.hospital_name,
        )
        sync_user_scope_from_staff(user, staff, preserve_higher_role=preserve_higher_role)
        if normalized_identifier and user.username != normalized_identifier.username:
            await _ensure_identifier_available(db, identifier=normalized_identifier, staff_id=staff.id)
            user.username = normalized_identifier.username
            changed = True
        after_scope = (
            user.role,
            user.hospital_code,
            user.hospital_name,
        )
        if before_scope != after_scope:
            changed = True
        if activate_existing and not user.is_active:
            user.is_active = True
            changed = True
            activated_existing = True
        if changed:
            await db.commit()
            await db.refresh(user)
        return StaffAccountProvisionResult(
            user=user,
            created=False,
            activated_existing=activated_existing,
            temporary_password=None,
            identifier=normalized_identifier,
        )

    identifier = resolve_staff_account_identifier(staff)
    await _ensure_identifier_available(db, identifier=identifier, staff_id=staff.id)

    temporary_password = build_default_password(identifier.username)
    user = User(
        username=identifier.username,
        hashed_password=hash_password(temporary_password),
        display_name=(staff.name or identifier.username).strip(),
        staff_id=staff.id,
        is_active=staff.is_active,
    )
    sync_user_scope_from_staff(user, staff, preserve_higher_role=preserve_higher_role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return StaffAccountProvisionResult(
        user=user,
        created=True,
        activated_existing=False,
        temporary_password=temporary_password,
        identifier=identifier,
    )


async def reset_staff_account_password(
    db: AsyncSession,
    *,
    staff: Staff,
) -> StaffAccountProvisionResult:
    user = await get_linked_user_by_staff_id(db, staff.id)
    if user is None:
        raise AccountProvisioningError("该员工尚未开通账号", status_code=404)

    temporary_password = build_default_password(user.username)
    user.hashed_password = hash_password(temporary_password)
    await db.commit()
    await db.refresh(user)
    from smart_badge_api.api.deps import invalidate_user_cache  # local import avoids cycle
    await invalidate_user_cache(user.id)
    return StaffAccountProvisionResult(
        user=user,
        created=False,
        activated_existing=False,
        temporary_password=temporary_password,
        identifier=None,
    )


async def set_staff_account_active(
    db: AsyncSession,
    *,
    staff: Staff,
    is_active: bool,
) -> User:
    user = await get_linked_user_by_staff_id(db, staff.id)
    if user is None:
        raise AccountProvisioningError("该员工尚未开通账号", status_code=404)

    if is_active and not staff.is_active:
        raise AccountProvisioningError("人员已禁用，不能启用登录账号", status_code=400)

    if user.is_active != is_active:
        user.is_active = is_active
        await db.commit()
        await db.refresh(user)
        from smart_badge_api.api.deps import invalidate_user_cache  # local import avoids cycle
        await invalidate_user_cache(user.id)
    return user
