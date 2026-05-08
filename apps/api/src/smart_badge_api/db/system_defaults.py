from __future__ import annotations

import os

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.db.models import AuditLog, PositionProfile, Staff, User

SUPER_ADMIN_POSITION_NAME = "超级管理员"
LEGACY_SUPER_ADMIN_POSITION_NAME = "超管"
INSTITUTION_ADMIN_POSITION_NAME = "机构管理员"
LEGACY_INSTITUTION_ADMIN_POSITION_NAME = "医院管理员"
SUPER_ADMIN_DISPLAY_NAME = "超级管理员"
LEGACY_SUPER_ADMIN_DISPLAY_NAME = "超管"
SUPER_ADMIN_NOTE = "平台最高权限，唯一账号使用"
SUPER_ADMIN_AUDIT_LOG_CONTENT = "新增用户：严伟哲 手机号：13709064973 性别：男 角色：超级管理员 权限范围：角色级别"
LEGACY_SUPER_ADMIN_AUDIT_LOG_CONTENT = "新增用户：严伟哲 手机号：13709064973 性别：男 角色：超管 权限范围：角色级别"

DEFAULT_POSITIONS = [
    {
        "name": SUPER_ADMIN_POSITION_NAME,
        "position_type": "management",
        "mapped_role": "super_admin",
        "is_super_admin": True,
        "note": SUPER_ADMIN_NOTE,
        "is_active": True,
    },
    {
        "name": "系统管理员",
        "position_type": "management",
        "mapped_role": "system_admin",
        "is_super_admin": False,
        "note": "全局系统管理与机构权限配置",
        "is_active": True,
    },
    {
        "name": INSTITUTION_ADMIN_POSITION_NAME,
        "position_type": "management",
        "mapped_role": "hospital_admin",
        "is_super_admin": False,
        "note": "管理机构范围内的员工与业务数据",
        "is_active": True,
    },
    {
        "name": "普通员工",
        "position_type": "staff",
        "mapped_role": "staff",
        "is_super_admin": False,
        "note": "一线接诊和客户服务岗位",
        "is_active": True,
    },
]

DEFAULT_STAFF = [
    {
        "name": "邓丽霞",
        "phone": "15729880982",
        "external_account": None,
        "gender": "female",
        "position_name": SUPER_ADMIN_POSITION_NAME,
        "badge_id": None,
        "is_active": True,
    },
    {
        "name": "咨询A",
        "phone": "15312345678",
        "external_account": "ABC123",
        "gender": "male",
        "position_name": "普通员工",
        "badge_id": None,
        "is_active": True,
    },
    {
        "name": "周蜜",
        "phone": "18380399820",
        "external_account": "8017104",
        "gender": "female",
        "position_name": INSTITUTION_ADMIN_POSITION_NAME,
        "badge_id": None,
        "is_active": True,
    },
]

DEFAULT_AUDIT_LOGS = [
    {
        "operator_name": "张万昕",
        "ip_address": "118.145.23.2",
        "module_name": "登录系统",
        "action_name": "账号密码登录",
        "content": "账号密码登录",
    },
    {
        "operator_name": "陈晨EC",
        "ip_address": "171.213.154.218",
        "module_name": "登录系统",
        "action_name": "账号密码登录",
        "content": "账号密码登录",
    },
    {
        "operator_name": "严伟哲",
        "ip_address": "223.87.33.97",
        "module_name": "登录系统",
        "action_name": "账号密码登录",
        "content": "账号密码登录",
    },
    {
        "operator_name": "言述-肖黎萍",
        "ip_address": "171.213.154.218",
        "module_name": "新增用户",
        "action_name": "人员管理",
        "content": SUPER_ADMIN_AUDIT_LOG_CONTENT,
    },
]

def _sample_staff_enabled() -> bool:
    value = os.getenv("SMART_BADGE_ENABLE_SAMPLE_STAFF", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _normalize_super_admin_position(position: PositionProfile) -> bool:
    changed = False

    if position.name != SUPER_ADMIN_POSITION_NAME:
        position.name = SUPER_ADMIN_POSITION_NAME
        changed = True
    if position.mapped_role != "super_admin":
        position.mapped_role = "super_admin"
        changed = True
    if not position.is_super_admin:
        position.is_super_admin = True
        changed = True
    if position.position_type != "management":
        position.position_type = "management"
        changed = True
    if position.note != SUPER_ADMIN_NOTE:
        position.note = SUPER_ADMIN_NOTE
        changed = True

    return changed


async def ensure_system_positions(db: AsyncSession) -> dict[str, PositionProfile]:
    changed = False

    result = await db.execute(select(PositionProfile))
    current_items = result.scalars().all()
    super_admin_position = next((item for item in current_items if item.name == SUPER_ADMIN_POSITION_NAME), None)
    legacy_super_admin_position = next((item for item in current_items if item.name == LEGACY_SUPER_ADMIN_POSITION_NAME), None)
    institution_admin_position = next(
        (item for item in current_items if item.name == INSTITUTION_ADMIN_POSITION_NAME or item.mapped_role == "hospital_admin"),
        None,
    )
    legacy_institution_admin_position = next(
        (item for item in current_items if item.name == LEGACY_INSTITUTION_ADMIN_POSITION_NAME),
        None,
    )

    if super_admin_position is not None:
        changed = _normalize_super_admin_position(super_admin_position) or changed

    if legacy_super_admin_position and super_admin_position is None:
        changed = _normalize_super_admin_position(legacy_super_admin_position) or changed
        super_admin_position = legacy_super_admin_position
        current_items = [super_admin_position if item.id == super_admin_position.id else item for item in current_items]

    if (
        super_admin_position
        and legacy_super_admin_position
        and legacy_super_admin_position.id != super_admin_position.id
    ):
        reassigned_staff = await db.execute(
            update(Staff)
            .where(Staff.position_id == legacy_super_admin_position.id)
            .values(position_id=super_admin_position.id)
        )
        if reassigned_staff.rowcount:
            changed = True
        await db.delete(legacy_super_admin_position)
        current_items = [item for item in current_items if item.id != legacy_super_admin_position.id]
        changed = True

    if institution_admin_position is not None:
        if institution_admin_position.name != INSTITUTION_ADMIN_POSITION_NAME:
            institution_admin_position.name = INSTITUTION_ADMIN_POSITION_NAME
            changed = True
        if institution_admin_position.position_type != "management":
            institution_admin_position.position_type = "management"
            changed = True
        if institution_admin_position.mapped_role != "hospital_admin":
            institution_admin_position.mapped_role = "hospital_admin"
            changed = True
        if institution_admin_position.note != "管理机构范围内的员工与业务数据":
            institution_admin_position.note = "管理机构范围内的员工与业务数据"
            changed = True

    if legacy_institution_admin_position and institution_admin_position is None:
        legacy_institution_admin_position.name = INSTITUTION_ADMIN_POSITION_NAME
        legacy_institution_admin_position.position_type = "management"
        legacy_institution_admin_position.mapped_role = "hospital_admin"
        legacy_institution_admin_position.note = "管理机构范围内的员工与业务数据"
        institution_admin_position = legacy_institution_admin_position
        current_items = [
            institution_admin_position if item.id == institution_admin_position.id else item
            for item in current_items
        ]
        changed = True

    if (
        institution_admin_position
        and legacy_institution_admin_position
        and legacy_institution_admin_position.id != institution_admin_position.id
    ):
        reassigned_staff = await db.execute(
            update(Staff)
            .where(Staff.position_id == legacy_institution_admin_position.id)
            .values(position_id=institution_admin_position.id)
        )
        if reassigned_staff.rowcount:
            changed = True
        await db.delete(legacy_institution_admin_position)
        current_items = [item for item in current_items if item.id != legacy_institution_admin_position.id]
        changed = True

    existing = {item.name: item for item in current_items}

    for item in DEFAULT_POSITIONS:
        if item["name"] in existing:
            continue
        position = PositionProfile(**item)
        db.add(position)
        existing[position.name] = position
        changed = True

    if changed:
        await db.commit()
    result = await db.execute(select(PositionProfile))
    existing = {item.name: item for item in result.scalars().all()}

    return existing


async def ensure_system_audit_logs(db: AsyncSession) -> None:
    count = (await db.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    if count > 0:
        updated_rows = await db.execute(
            update(AuditLog)
            .where(AuditLog.content.like(f"%{LEGACY_SUPER_ADMIN_POSITION_NAME}%"))
            .values(
                content=func.replace(
                    AuditLog.content,
                    LEGACY_SUPER_ADMIN_POSITION_NAME,
                    SUPER_ADMIN_POSITION_NAME,
                )
            )
        )
        if updated_rows.rowcount:
            await db.commit()
        return

    for item in DEFAULT_AUDIT_LOGS:
        db.add(AuditLog(**item))
    await db.commit()


async def ensure_system_sample_staff(db: AsyncSession) -> None:
    if not _sample_staff_enabled():
        return

    count = (await db.execute(select(func.count()).select_from(Staff))).scalar_one()
    if count > 0:
        return

    positions = await ensure_system_positions(db)
    required_position_names = {item["position_name"] for item in DEFAULT_STAFF}

    if not required_position_names.issubset(positions.keys()):
        return

    for sample in DEFAULT_STAFF:
        position = positions[sample["position_name"]]
        db.add(
            Staff(
                name=sample["name"],
                phone=sample["phone"],
                external_account=sample["external_account"],
                gender=sample["gender"],
                position_id=position.id,
                role="consultant",
                permission_role=position.mapped_role,
                badge_id=sample["badge_id"],
                is_active=sample["is_active"],
            )
        )

    await db.commit()

async def ensure_system_admin_account(db: AsyncSession) -> None:
    admin = (await db.execute(select(User).where(User.username == "admin"))).scalar_one_or_none()
    if admin is None or admin.display_name != LEGACY_SUPER_ADMIN_DISPLAY_NAME:
        return

    admin.display_name = SUPER_ADMIN_DISPLAY_NAME
    await db.commit()


async def ensure_system_management_defaults(db: AsyncSession) -> None:
    await ensure_system_positions(db)
    await ensure_system_sample_staff(db)
    await ensure_system_admin_account(db)
    await ensure_system_audit_logs(db)
