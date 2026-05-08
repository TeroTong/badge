from __future__ import annotations

from typing import Any

from smart_badge_api.core.permissions import PermissionScope, normalize_permission_role

_ARCHIVE_GLOBAL_ROLES = {"super_admin", "system_admin"}


def _clean_text(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def archive_effective_staff_id(requested_staff_id: str | None, user: object) -> str | None:
    role = normalize_permission_role(getattr(user, "role", None))
    normalized_requested_staff_id = _clean_text(requested_staff_id)
    if role in _ARCHIVE_GLOBAL_ROLES:
        return normalized_requested_staff_id
    if role == "hospital_admin":
        return normalized_requested_staff_id
    return _clean_text(getattr(user, "staff_id", None))


def _archive_item_staff_id(item: dict[str, Any]) -> str | None:
    return _clean_text(item.get("staff_id"))


def archive_item_hospital_code(item: dict[str, Any]) -> str | None:
    for key in (
        "staff_hospital_code",
        "hospital_code",
        "device_hospital_code",
        "staffHospitalCode",
        "hospitalCode",
        "deviceHospitalCode",
    ):
        value = _clean_text(item.get(key))
        if value:
            return value
    return None


def _archive_item_visible(
    item: dict[str, Any],
    *,
    role: str | None,
    staff_id: str | None,
    hospital_code: str | None,
) -> bool:
    normalized_role = normalize_permission_role(role)
    if normalized_role in _ARCHIVE_GLOBAL_ROLES:
        return True

    scoped_staff_id = _clean_text(staff_id)
    if normalized_role == "hospital_admin":
        scoped_hospital_code = _clean_text(hospital_code)
        if not scoped_hospital_code:
            return False
        item_hospital_code = archive_item_hospital_code(item)
        if item_hospital_code:
            return item_hospital_code == scoped_hospital_code
        # Very old archive items may not have hospital metadata. Keep those
        # conservative: show only the admin's own recordings instead of leaking
        # other institutions' historical archive rows.
        return bool(scoped_staff_id and _archive_item_staff_id(item) == scoped_staff_id)

    if not scoped_staff_id:
        return False
    return _archive_item_staff_id(item) == scoped_staff_id


def archive_item_visible_to_user(item: dict[str, Any], user: object) -> bool:
    return _archive_item_visible(
        item,
        role=getattr(user, "role", None),
        staff_id=getattr(user, "staff_id", None),
        hospital_code=getattr(user, "hospital_code", None),
    )


def archive_item_visible_to_scope(item: dict[str, Any], scope: PermissionScope) -> bool:
    return _archive_item_visible(
        item,
        role=scope.role,
        staff_id=scope.staff_id,
        hospital_code=scope.hospital_code,
    )
