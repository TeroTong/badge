from __future__ import annotations

from dataclasses import dataclass


PERMISSION_ROLE_LEVELS: dict[str, int] = {
    "staff": 10,
    "hospital_admin": 30,
    "system_admin": 90,
    "super_admin": 100,
}

PERMISSION_ROLE_LABELS: dict[str, str] = {
    "super_admin": "超级管理员",
    "system_admin": "系统管理员",
    "hospital_admin": "机构管理员",
    "staff": "普通员工",
}

LEGACY_USER_ROLE_MAP: dict[str, str] = {
    "admin": "system_admin",
    "manager": "hospital_admin",
    "viewer": "staff",
}

LEGACY_STAFF_PERMISSION_ROLE_MAP: dict[str, str] = {
    "admin": "system_admin",
    "manager": "hospital_admin",
    "consultant": "staff",
}

GLOBAL_ROLES = {"super_admin", "system_admin"}


@dataclass(slots=True)
class PermissionScope:
    role: str
    staff_id: str | None = None
    hospital_code: str | None = None


def normalize_permission_role(role: str | None) -> str:
    normalized = (role or "").strip()
    if not normalized:
        return "staff"
    if normalized in PERMISSION_ROLE_LEVELS:
        return normalized
    if normalized in LEGACY_USER_ROLE_MAP:
        return LEGACY_USER_ROLE_MAP[normalized]
    if normalized in LEGACY_STAFF_PERMISSION_ROLE_MAP:
        return LEGACY_STAFF_PERMISSION_ROLE_MAP[normalized]
    return "staff"


def permission_role_level(role: str | None) -> int:
    return PERMISSION_ROLE_LEVELS.get(normalize_permission_role(role), 0)


def is_global_role(role: str | None) -> bool:
    return normalize_permission_role(role) in GLOBAL_ROLES


def is_super_admin(role: str | None) -> bool:
    return normalize_permission_role(role) == "super_admin"


def can_manage_role(actor_role: str | None, target_role: str | None) -> bool:
    actor = normalize_permission_role(actor_role)
    target = normalize_permission_role(target_role)
    if target == "super_admin":
        return actor == "super_admin"
    return permission_role_level(actor) > permission_role_level(target)


def role_requires_hospital(role: str | None) -> bool:
    return normalize_permission_role(role) == "hospital_admin"
