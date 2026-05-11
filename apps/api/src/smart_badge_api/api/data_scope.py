from __future__ import annotations

from sqlalchemy import case, exists, false, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from smart_badge_api.core.permissions import (
    LEGACY_STAFF_PERMISSION_ROLE_MAP,
    PERMISSION_ROLE_LEVELS,
    PermissionScope,
    GLOBAL_ROLES,
    normalize_permission_role,
    permission_role_level,
)
from smart_badge_api.db.models import Customer, Recording, RecordingVisitLink, Staff, StaffManagementRelation, User, Visit, VisitOrder


_STAFF_PERMISSION_ROLE_LEVELS = {
    **PERMISSION_ROLE_LEVELS,
    **{
        legacy_role: PERMISSION_ROLE_LEVELS[normalized_role]
        for legacy_role, normalized_role in LEGACY_STAFF_PERMISSION_ROLE_MAP.items()
    },
}


def _first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value and value.strip():
            return value.strip()
    return None


async def build_permission_scope(user: User) -> PermissionScope:
    role = normalize_permission_role(getattr(user, "role", None))
    return PermissionScope(
        role=role,
        staff_id=getattr(user, "staff_id", None),
        hospital_code=None if role in GLOBAL_ROLES else _first_non_empty(getattr(user, "hospital_code", None)),
    )


def _staff_visit_order_participation_condition(staff_model, visit_order_model) -> ColumnElement[bool]:
    return or_(
        staff_model.external_account == visit_order_model.fzuer,
        staff_model.external_account == visit_order_model.d_fzuer,
        staff_model.external_account == visit_order_model.fzr_id_dq,
        staff_model.external_account == visit_order_model.advxc,
        staff_model.external_account == visit_order_model.assxc,
        staff_model.external_account == visit_order_model.advyq,
        staff_model.external_account == visit_order_model.yyuer,
        staff_model.external_account == visit_order_model.vipkf,
        staff_model.external_account == visit_order_model.d_vipkf,
    )


def _clean_staff_id(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _staff_role_level_condition(scope: PermissionScope, staff_model) -> ColumnElement[bool]:
    role = normalize_permission_role(scope.role)
    if role == "super_admin":
        return true()
    return (
        case(
            _STAFF_PERMISSION_ROLE_LEVELS,
            value=staff_model.permission_role,
            else_=PERMISSION_ROLE_LEVELS["staff"],
        )
        <= permission_role_level(role)
    )


def _staff_id_within_role_ceiling(scope: PermissionScope, staff_id_column) -> ColumnElement[bool]:
    role = normalize_permission_role(scope.role)
    if role == "super_admin":
        return true()

    scoped_staff = aliased(Staff)
    return exists(
        select(scoped_staff.id).where(
            scoped_staff.id == staff_id_column,
            scoped_staff.is_active.is_(True),
            _staff_role_level_condition(scope, scoped_staff),
        )
    )


async def resolve_visible_staff_ids_for_user(db: AsyncSession | None, user: object) -> set[str] | None:
    role = normalize_permission_role(getattr(user, "role", None))
    staff_id = _clean_staff_id(getattr(user, "staff_id", None))
    if role == "super_admin":
        return None
    if role == "system_admin":
        if db is None:
            return {staff_id} if staff_id else set()
        rows = (
            await db.execute(
                select(Staff.id).where(
                    Staff.is_active.is_(True),
                    _staff_role_level_condition(PermissionScope(role=role), Staff),
                )
            )
        ).scalars().all()
        visible_staff_ids = {item for item in rows if item}
        if staff_id:
            visible_staff_ids.add(staff_id)
        return visible_staff_ids
    if not staff_id:
        return set()
    if db is None:
        return {staff_id}

    rows = (
        await db.execute(
            select(Staff.id, Staff.permission_role)
            .join(StaffManagementRelation, StaffManagementRelation.subordinate_staff_id == Staff.id)
            .where(
                StaffManagementRelation.manager_staff_id == staff_id,
                Staff.is_active.is_(True),
            )
        )
    ).all()
    visible_staff_ids = {staff_id}
    actor_level = permission_role_level(role)
    for subordinate_staff_id, subordinate_role in rows:
        if subordinate_staff_id == staff_id or permission_role_level(subordinate_role) <= actor_level:
            visible_staff_ids.add(subordinate_staff_id)
    return visible_staff_ids


def _staff_id_in_management_scope(scope: PermissionScope, staff_id_column) -> ColumnElement[bool]:
    role = normalize_permission_role(scope.role)
    if role == "super_admin":
        return true()
    if role == "system_admin":
        return _staff_id_within_role_ceiling(scope, staff_id_column)
    if not scope.staff_id:
        return false()
    if scope.role == "single_staff":
        return staff_id_column == scope.staff_id
    managed_staff = aliased(Staff)
    return or_(
        staff_id_column == scope.staff_id,
        exists(
            select(StaffManagementRelation.id)
            .join(managed_staff, managed_staff.id == StaffManagementRelation.subordinate_staff_id)
            .where(
                StaffManagementRelation.manager_staff_id == scope.staff_id,
                StaffManagementRelation.subordinate_staff_id == staff_id_column,
                managed_staff.is_active.is_(True),
                _staff_role_level_condition(scope, managed_staff),
            )
        ),
    )


def managed_staff_scope_condition(scope: PermissionScope, staff_id_column) -> ColumnElement[bool]:
    return _staff_id_in_management_scope(scope, staff_id_column)


def _hospital_visit_match_condition(
    scope: PermissionScope,
    *,
    visit_model,
    visit_order_model,
    consultant_model,
    doctor_model,
) -> ColumnElement[bool]:
    return or_(
        exists(
            select(visit_order_model.id).where(
                visit_order_model.dzdh == visit_model.external_visit_order_no,
                visit_order_model.jgbm == scope.hospital_code,
            )
        ),
        exists(
            select(consultant_model.id).where(
                consultant_model.id == visit_model.consultant_id,
                consultant_model.hospital_code == scope.hospital_code,
            )
        ),
        exists(
            select(doctor_model.id).where(
                doctor_model.id == visit_model.doctor_id,
                doctor_model.hospital_code == scope.hospital_code,
            )
        ),
    )


def visit_scope_condition(scope: PermissionScope) -> ColumnElement[bool]:
    role = normalize_permission_role(scope.role)
    if role == "super_admin":
        return true()
    if role == "system_admin" or scope.staff_id:
        direct_recording_model = aliased(Recording)
        linked_recording_model = aliased(Recording)
        visit_order_model = aliased(VisitOrder)
        staff_model = aliased(Staff)
        return or_(
            _staff_id_in_management_scope(scope, Visit.consultant_id),
            _staff_id_in_management_scope(scope, Visit.doctor_id),
            exists(
                select(visit_order_model.id)
                .select_from(visit_order_model, staff_model)
                .where(
                    _staff_id_in_management_scope(scope, staff_model.id),
                    staff_model.external_account.is_not(None),
                    Visit.external_visit_order_no.is_not(None),
                    visit_order_model.dzdh == Visit.external_visit_order_no,
                    staff_model.hospital_code.is_not(None),
                    visit_order_model.jgbm == staff_model.hospital_code,
                    _staff_visit_order_participation_condition(staff_model, visit_order_model),
                )
            ),
            exists(
                select(direct_recording_model.id).where(
                    direct_recording_model.visit_id == Visit.id,
                    _staff_id_in_management_scope(scope, direct_recording_model.staff_id),
                )
            ),
            exists(
                select(RecordingVisitLink.id)
                .join(linked_recording_model, linked_recording_model.id == RecordingVisitLink.recording_id)
                .where(
                    RecordingVisitLink.visit_id == Visit.id,
                    _staff_id_in_management_scope(scope, linked_recording_model.staff_id),
                )
            ),
        )

    return false()


def recording_scope_condition(scope: PermissionScope) -> ColumnElement[bool]:
    role = normalize_permission_role(scope.role)
    if role == "super_admin":
        return true()
    if role == "system_admin" or scope.staff_id:
        return _staff_id_in_management_scope(scope, Recording.staff_id)

    return false()


def customer_scope_condition(scope: PermissionScope) -> ColumnElement[bool]:
    role = normalize_permission_role(scope.role)
    if role == "super_admin":
        return true()
    return exists(
        select(Visit.id).where(
            Visit.customer_id == Customer.id,
            visit_scope_condition(scope),
        )
    )


def visit_order_scope_condition(scope: PermissionScope) -> ColumnElement[bool]:
    role = normalize_permission_role(scope.role)
    if role == "super_admin":
        return true()
    if role == "system_admin" or scope.staff_id:
        participant_visit_order = aliased(VisitOrder)
        participant_staff = aliased(Staff)
        return exists(
            select(participant_visit_order.id)
            .select_from(participant_visit_order, participant_staff)
            .where(
                _staff_id_in_management_scope(scope, participant_staff.id),
                participant_staff.external_account.is_not(None),
                participant_staff.hospital_code.is_not(None),
                participant_staff.hospital_code == VisitOrder.jgbm,
                participant_visit_order.dzdh == VisitOrder.dzdh,
                participant_visit_order.jgbm == VisitOrder.jgbm,
                _staff_visit_order_participation_condition(participant_staff, participant_visit_order),
            )
        )

    return false()


def staff_scope_condition(scope: PermissionScope) -> ColumnElement[bool]:
    role = normalize_permission_role(scope.role)
    if role == "super_admin":
        return true_condition()
    if role == "system_admin":
        return _staff_role_level_condition(scope, Staff)
    if scope.role == "hospital_admin":
        return Staff.hospital_code == scope.hospital_code if scope.hospital_code else false()
    if scope.staff_id:
        return _staff_id_in_management_scope(scope, Staff.id)
    return false()


def true_condition() -> ColumnElement[bool]:
    return true()
