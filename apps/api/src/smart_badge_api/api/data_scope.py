from __future__ import annotations

from sqlalchemy import and_, case, exists, false, func, or_, select, true
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from smart_badge_api.core.config import get_settings
from smart_badge_api.core.permissions import (
    LEGACY_STAFF_PERMISSION_ROLE_MAP,
    PERMISSION_ROLE_LEVELS,
    PermissionScope,
    GLOBAL_ROLES,
    is_global_role,
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


def _recording_created_date_text(recording_model) -> ColumnElement[str]:
    if get_settings().database_url.startswith("sqlite"):
        return func.date(recording_model.created_at, "+8 hours")
    return func.to_char(func.timezone("Asia/Shanghai", recording_model.created_at), "YYYY-MM-DD")


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


def _staff_id_in_management_scope(scope: PermissionScope, staff_id_column) -> ColumnElement[bool]:
    if normalize_permission_role(scope.role) in GLOBAL_ROLES:
        return true()
    if not scope.staff_id:
        return false()
    if scope.role == "single_staff":
        return staff_id_column == scope.staff_id
    managed_staff = aliased(Staff)
    role = normalize_permission_role(scope.role)
    actor_level = permission_role_level(role)
    managed_role_allowed = (
        true()
        if role == "super_admin"
        else case(
            _STAFF_PERMISSION_ROLE_LEVELS,
            value=managed_staff.permission_role,
            else_=PERMISSION_ROLE_LEVELS["staff"],
        )
        <= actor_level
    )
    return or_(
        staff_id_column == scope.staff_id,
        exists(
            select(StaffManagementRelation.id)
            .join(managed_staff, managed_staff.id == StaffManagementRelation.subordinate_staff_id)
            .where(
                StaffManagementRelation.manager_staff_id == scope.staff_id,
                StaffManagementRelation.subordinate_staff_id == staff_id_column,
                managed_staff.is_active.is_(True),
                managed_role_allowed,
            )
        ),
    )


def managed_staff_scope_condition(scope: PermissionScope, staff_id_column) -> ColumnElement[bool]:
    return _staff_id_in_management_scope(scope, staff_id_column)


def _staff_visit_order_recording_date_condition(scope: PermissionScope, visit_order_model) -> ColumnElement[bool]:
    return exists(
        select(Recording.id).where(
            _staff_id_in_management_scope(scope, Recording.staff_id),
            Recording.created_at.is_not(None),
            or_(
                _recording_created_date_text(Recording) == visit_order_model.crtdt,
                _recording_created_date_text(Recording) == visit_order_model.sjrq,
            ),
        )
    )


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
    if normalize_permission_role(scope.role) in GLOBAL_ROLES:
        return true()
    if scope.staff_id:
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
    if normalize_permission_role(scope.role) in GLOBAL_ROLES:
        return true()
    if scope.staff_id:
        return _staff_id_in_management_scope(scope, Recording.staff_id)

    return false()


def customer_scope_condition(scope: PermissionScope) -> ColumnElement[bool]:
    if normalize_permission_role(scope.role) in GLOBAL_ROLES:
        return true()
    return exists(
        select(Visit.id).where(
            Visit.customer_id == Customer.id,
            visit_scope_condition(scope),
        )
    )


def visit_order_scope_condition(scope: PermissionScope) -> ColumnElement[bool]:
    if normalize_permission_role(scope.role) in GLOBAL_ROLES:
        return true()
    if scope.staff_id:
        participant_visit_order = aliased(VisitOrder)
        participant_staff = aliased(Staff)
        return and_(
            exists(
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
            ),
            _staff_visit_order_recording_date_condition(scope, VisitOrder),
        )

    return false()


def staff_scope_condition(scope: PermissionScope) -> ColumnElement[bool]:
    if is_global_role(scope.role):
        return true_condition()
    if scope.role == "hospital_admin":
        return Staff.hospital_code == scope.hospital_code if scope.hospital_code else false()
    if scope.staff_id:
        return _staff_id_in_management_scope(scope, Staff.id)
    return false()


def true_condition() -> ColumnElement[bool]:
    return true()
