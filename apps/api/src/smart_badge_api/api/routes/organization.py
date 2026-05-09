from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.audit import append_audit_log
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.core.permissions import is_global_role, normalize_permission_role, permission_role_level
from smart_badge_api.db.models import (
    OrganizationUnit,
    OrganizationUnitMember,
    PositionProfile,
    Staff,
    StaffManagementRelation,
    User,
    WecomTenant,
)
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.organization import (
    OrganizationOverviewOut,
    OrganizationStaffOut,
    OrganizationUnitCreate,
    OrganizationUnitMemberMove,
    OrganizationUnitMemberOut,
    OrganizationUnitMemberUpdate,
    OrganizationUnitOut,
    OrganizationUnitUpdate,
    StaffManagementRelationByUnitCreate,
    StaffManagementRelationBulkCreate,
    StaffManagementRelationCreate,
    StaffManagementRelationOut,
    StaffManagementRelationSync,
)

router = APIRouter(prefix="/organization", tags=["组织架构"])


def _clean_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _operator_name(user: User) -> str:
    return user.display_name or user.username or "系统"


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


async def _resolve_hospital_name(db: AsyncSession, hospital_code: str) -> str | None:
    tenant_name = (
        await db.execute(
            select(WecomTenant.name)
            .where(
                WecomTenant.default_hospital_code == hospital_code,
                WecomTenant.default_hospital_code.is_not(None),
                WecomTenant.default_hospital_code != "",
            )
            .order_by(WecomTenant.is_active.desc(), WecomTenant.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if _clean_text(tenant_name):
        return _clean_text(tenant_name)

    staff_hospital_name = (
        await db.execute(
            select(func.max(Staff.hospital_short_name)).where(
                Staff.hospital_code == hospital_code,
                Staff.hospital_short_name.is_not(None),
                Staff.hospital_short_name != "",
            )
        )
    ).scalar_one_or_none()
    return _clean_text(staff_hospital_name)


async def _pick_default_hospital_code(db: AsyncSession, user: User) -> str | None:
    if not is_global_role(user.role) and _clean_text(user.hospital_code):
        return _clean_text(user.hospital_code)

    tenant_code = (
        await db.execute(
            select(WecomTenant.default_hospital_code)
            .where(
                WecomTenant.default_hospital_code.is_not(None),
                WecomTenant.default_hospital_code != "",
            )
            .order_by(WecomTenant.is_default.desc(), WecomTenant.is_active.desc(), WecomTenant.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if _clean_text(tenant_code):
        return _clean_text(tenant_code)

    staff_code = (
        await db.execute(
            select(Staff.hospital_code)
            .where(Staff.hospital_code.is_not(None), Staff.hospital_code != "")
            .group_by(Staff.hospital_code)
            .order_by(func.count(Staff.id).desc(), Staff.hospital_code.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return _clean_text(staff_code)


async def _resolve_request_hospital(
    db: AsyncSession,
    user: User,
    hospital_code: str | None,
) -> tuple[str, str | None]:
    requested_code = _clean_text(hospital_code)
    if is_global_role(user.role):
        code = requested_code or await _pick_default_hospital_code(db, user)
        if not code:
            raise HTTPException(400, "请先选择机构")
        return code, await _resolve_hospital_name(db, code)

    user_code = _clean_text(user.hospital_code)
    if not user_code:
        raise HTTPException(403, "当前账号未绑定机构，无法配置组织架构")
    if requested_code and requested_code != user_code:
        raise HTTPException(403, "只能配置本机构的组织架构")
    return user_code, await _resolve_hospital_name(db, user_code)


def _assert_unit_access(user: User, unit: OrganizationUnit) -> None:
    if is_global_role(user.role):
        return
    if not user.hospital_code or unit.hospital_code != user.hospital_code:
        raise HTTPException(404, "组织不存在")


def _staff_role_level(staff: Staff) -> int:
    return permission_role_level(staff.permission_role)


def _relation_actor_level(user: User) -> int:
    return permission_role_level(user.role)


def _can_configure_relation_manager(user: User, manager: Staff) -> bool:
    if normalize_permission_role(user.role) == "super_admin":
        return True
    if is_global_role(user.role):
        return _staff_role_level(manager) <= _relation_actor_level(user)
    if not user.hospital_code or manager.hospital_code != user.hospital_code:
        return False
    if normalize_permission_role(user.role) == "hospital_admin":
        return _staff_role_level(manager) <= _relation_actor_level(user)
    return bool(user.staff_id and manager.id == user.staff_id)


def _assert_relation_manager_access(user: User, manager: Staff) -> None:
    if not _can_configure_relation_manager(user, manager):
        raise HTTPException(403, "无权查看或修改该人员的管理范围")


def _can_configure_relation_target(user: User, staff: Staff) -> bool:
    if normalize_permission_role(user.role) == "super_admin":
        return True
    if is_global_role(user.role):
        return _staff_role_level(staff) <= _relation_actor_level(user)
    if not user.hospital_code or staff.hospital_code != user.hospital_code:
        return False
    if user.staff_id and staff.id == user.staff_id:
        return True
    return _staff_role_level(staff) <= _relation_actor_level(user)


def _filter_relation_target_staff_map(
    user: User,
    *,
    manager: Staff,
    staff_map: dict[str, Staff],
) -> dict[str, Staff]:
    forbidden = [
        staff.name
        for staff in staff_map.values()
        if staff.id != manager.id and staff.is_active and not _can_configure_relation_target(user, staff)
    ]
    if forbidden:
        raise HTTPException(403, f"无权将更高权限人员加入管理范围：{'、'.join(forbidden[:5])}")
    return {
        staff_id: staff
        for staff_id, staff in staff_map.items()
        if staff_id == manager.id or (staff.is_active and _can_configure_relation_target(user, staff))
    }


def _can_view_management_relation(
    user: User,
    *,
    manager_staff_id: str,
    manager_hospital_code: str | None,
    manager_permission_role: str | None,
    subordinate_staff_id: str,
    subordinate_hospital_code: str | None,
    subordinate_permission_role: str | None,
) -> bool:
    if normalize_permission_role(user.role) == "super_admin":
        return True
    actor_level = _relation_actor_level(user)
    if is_global_role(user.role):
        return (
            permission_role_level(manager_permission_role) <= actor_level
            and permission_role_level(subordinate_permission_role) <= actor_level
        )
    if (
        not user.hospital_code
        or manager_hospital_code != user.hospital_code
        or subordinate_hospital_code != user.hospital_code
    ):
        return False

    manager_allowed = (
        normalize_permission_role(user.role) == "hospital_admin"
        and permission_role_level(manager_permission_role) <= actor_level
    ) or bool(user.staff_id and manager_staff_id == user.staff_id)
    subordinate_allowed = bool(user.staff_id and subordinate_staff_id == user.staff_id) or (
        permission_role_level(subordinate_permission_role) <= actor_level
    )
    return manager_allowed and subordinate_allowed


async def _get_unit_or_404(db: AsyncSession, unit_id: str, user: User) -> OrganizationUnit:
    unit = await db.get(OrganizationUnit, unit_id)
    if unit is None:
        raise HTTPException(404, "组织不存在")
    _assert_unit_access(user, unit)
    return unit


async def _load_units(db: AsyncSession, hospital_code: str) -> list[OrganizationUnit]:
    return (
        await db.execute(
            select(OrganizationUnit)
            .where(OrganizationUnit.hospital_code == hospital_code)
            .order_by(OrganizationUnit.parent_id.asc().nullsfirst(), OrganizationUnit.sort_order.asc(), OrganizationUnit.name.asc())
        )
    ).scalars().all()


def _unit_path(unit: OrganizationUnit, unit_map: dict[str, OrganizationUnit]) -> str:
    names: list[str] = []
    current: OrganizationUnit | None = unit
    seen: set[str] = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        names.append(current.name)
        current = unit_map.get(current.parent_id or "")
    return " / ".join(reversed(names))


def _would_create_unit_cycle(
    *,
    unit_id: str,
    next_parent_id: str | None,
    unit_map: dict[str, OrganizationUnit],
) -> bool:
    current_id = next_parent_id
    seen: set[str] = set()
    while current_id:
        if current_id == unit_id:
            return True
        if current_id in seen:
            return True
        seen.add(current_id)
        current = unit_map.get(current_id)
        current_id = current.parent_id if current else None
    return False


async def _assert_unique_unit_name(
    db: AsyncSession,
    *,
    hospital_code: str,
    parent_id: str | None,
    name: str,
    exclude_unit_id: str | None = None,
) -> None:
    stmt = select(OrganizationUnit.id).where(
        OrganizationUnit.hospital_code == hospital_code,
        OrganizationUnit.name == name,
    )
    if parent_id:
        stmt = stmt.where(OrganizationUnit.parent_id == parent_id)
    else:
        stmt = stmt.where(OrganizationUnit.parent_id.is_(None))
    if exclude_unit_id:
        stmt = stmt.where(OrganizationUnit.id != exclude_unit_id)
    if (await db.execute(stmt.limit(1))).scalar_one_or_none():
        raise HTTPException(400, "同一上级下已存在同名组织")


async def _validate_parent(
    db: AsyncSession,
    *,
    hospital_code: str,
    parent_id: str | None,
    current_unit_id: str | None = None,
) -> str | None:
    normalized_parent_id = _clean_text(parent_id)
    if not normalized_parent_id:
        return None

    parent = await db.get(OrganizationUnit, normalized_parent_id)
    if parent is None or parent.hospital_code != hospital_code:
        raise HTTPException(400, "上级组织不存在或不属于当前机构")
    if current_unit_id:
        units = await _load_units(db, hospital_code)
        unit_map = {item.id: item for item in units}
        if _would_create_unit_cycle(
            unit_id=current_unit_id,
            next_parent_id=normalized_parent_id,
            unit_map=unit_map,
        ):
            raise HTTPException(400, "上级组织不能设置为自身或下级组织")
    return normalized_parent_id


async def _load_staff_map(
    db: AsyncSession,
    *,
    hospital_code: str,
    staff_ids: list[str],
) -> dict[str, Staff]:
    normalized_ids = [item for item in dict.fromkeys(_clean_text(item) for item in staff_ids) if item]
    if not normalized_ids:
        return {}
    rows = (
        await db.execute(
            select(Staff).where(
                Staff.id.in_(normalized_ids),
                Staff.hospital_code == hospital_code,
            )
        )
    ).scalars().all()
    staff_map = {item.id: item for item in rows}
    missing = [item for item in normalized_ids if item not in staff_map]
    if missing:
        raise HTTPException(400, "所选人员不存在或不属于当前机构")
    return staff_map


async def _would_create_management_cycle(
    db: AsyncSession,
    *,
    hospital_code: str,
    manager_staff_id: str,
    subordinate_staff_id: str,
) -> bool:
    if manager_staff_id == subordinate_staff_id:
        return False

    rows = (
        await db.execute(
            select(
                StaffManagementRelation.manager_staff_id,
                StaffManagementRelation.subordinate_staff_id,
            ).where(StaffManagementRelation.hospital_code == hospital_code)
        )
    ).all()
    children: dict[str, set[str]] = defaultdict(set)
    for manager_id, subordinate_id in rows:
        children[manager_id].add(subordinate_id)
    children[manager_staff_id].add(subordinate_staff_id)

    stack = [subordinate_staff_id]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current == manager_staff_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(children.get(current, set()))
    return False


async def _create_management_relations_for_staff_ids(
    db: AsyncSession,
    *,
    hospital_code: str,
    manager: Staff,
    subordinate_staff_ids: list[str],
    current_user: User,
) -> tuple[list[StaffManagementRelation], dict[str, Staff]]:
    staff_map = await _load_staff_map(db, hospital_code=hospital_code, staff_ids=subordinate_staff_ids)
    target_staff_map = _filter_relation_target_staff_map(
        current_user,
        manager=manager,
        staff_map={
            staff_id: staff
            for staff_id, staff in staff_map.items()
            if staff_id != manager.id and staff.is_active
        },
    )
    target_staff_map = {
        staff_id: staff
        for staff_id, staff in target_staff_map.items()
        if staff_id != manager.id and staff.is_active
    }
    if not target_staff_map:
        return [], {}

    target_staff_ids = list(target_staff_map.keys())
    existing_subordinate_ids = set(
        (
            await db.execute(
                select(StaffManagementRelation.subordinate_staff_id).where(
                    StaffManagementRelation.manager_staff_id == manager.id,
                    StaffManagementRelation.subordinate_staff_id.in_(target_staff_ids),
                )
            )
        ).scalars().all()
    )
    create_staff_ids = [staff_id for staff_id in target_staff_ids if staff_id not in existing_subordinate_ids]
    if not create_staff_ids:
        return [], target_staff_map

    for subordinate_id in create_staff_ids:
        if await _would_create_management_cycle(
            db,
            hospital_code=hospital_code,
            manager_staff_id=manager.id,
            subordinate_staff_id=subordinate_id,
        ):
            raise HTTPException(400, f"该管理关系会形成循环：{manager.name} 管理 {target_staff_map[subordinate_id].name}")

    created_relations: list[StaffManagementRelation] = []
    for subordinate_id in create_staff_ids:
        relation = StaffManagementRelation(
            hospital_code=hospital_code,
            manager_staff_id=manager.id,
            subordinate_staff_id=subordinate_id,
        )
        db.add(relation)
        created_relations.append(relation)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(400, "管理关系创建失败，请刷新后重试") from exc
    return created_relations, target_staff_map


def _collect_unit_subtree_ids(
    units: list[OrganizationUnit],
    unit_id: str,
    *,
    include_self: bool = True,
) -> list[str]:
    children_map: dict[str, list[str]] = defaultdict(list)
    for unit in units:
        if unit.parent_id:
            children_map[unit.parent_id].append(unit.id)

    result: list[str] = [unit_id] if include_self else []
    seen: set[str] = set(result)
    stack = list(children_map.get(unit_id, []))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        result.append(current)
        stack.extend(children_map.get(current, []))
    return result


def _build_subtree_member_counts(
    units: list[OrganizationUnit],
    membership_rows: list[tuple[OrganizationUnitMember, Staff, str | None]],
) -> dict[str, int]:
    children_map: dict[str, list[str]] = defaultdict(list)
    direct_staff_ids: dict[str, set[str]] = defaultdict(set)
    for unit in units:
        if unit.parent_id:
            children_map[unit.parent_id].append(unit.id)
    for membership, staff, _position_name in membership_rows:
        direct_staff_ids[membership.unit_id].add(staff.id)

    memo: dict[str, set[str]] = {}

    def collect(unit_id: str) -> set[str]:
        if unit_id in memo:
            return memo[unit_id]
        staff_ids = set(direct_staff_ids.get(unit_id, set()))
        for child_id in children_map.get(unit_id, []):
            staff_ids.update(collect(child_id))
        memo[unit_id] = staff_ids
        return staff_ids

    return {unit.id: len(collect(unit.id)) for unit in units}


def _to_unit_out(
    unit: OrganizationUnit,
    *,
    unit_map: dict[str, OrganizationUnit],
    member_counts: dict[str, int],
) -> OrganizationUnitOut:
    return OrganizationUnitOut(
        id=unit.id,
        hospital_code=unit.hospital_code,
        hospital_name=unit.hospital_name,
        name=unit.name,
        parent_id=unit.parent_id,
        path=_unit_path(unit, unit_map),
        sort_order=unit.sort_order,
        member_count=member_counts.get(unit.id, 0),
        is_active=unit.is_active,
        created_at=unit.created_at,
        updated_at=unit.updated_at,
    )


@router.get("/overview", response_model=OrganizationOverviewOut)
async def get_organization_overview(
    hospital_code: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    resolved_hospital_code, hospital_name = await _resolve_request_hospital(db, current_user, hospital_code)
    units = await _load_units(db, resolved_hospital_code)
    unit_map = {item.id: item for item in units}

    staff_rows = (
        await db.execute(
            select(Staff, PositionProfile.name)
            .outerjoin(PositionProfile, PositionProfile.id == Staff.position_id)
            .where(Staff.hospital_code == resolved_hospital_code)
            .order_by(Staff.is_active.desc(), Staff.name.asc(), Staff.external_account.asc())
        )
    ).all()
    staff_items = [
        OrganizationStaffOut(
            id=staff.id,
            name=staff.name,
            external_account=staff.external_account,
            hospital_code=staff.hospital_code,
            hospital_short_name=staff.hospital_short_name,
            position_id=staff.position_id,
            position_name=position_name,
            permission_role=normalize_permission_role(staff.permission_role),
            is_active=staff.is_active,
        )
        for staff, position_name in staff_rows
    ]

    membership_rows = (
        await db.execute(
            select(OrganizationUnitMember, Staff, PositionProfile.name)
            .join(OrganizationUnit, OrganizationUnit.id == OrganizationUnitMember.unit_id)
            .join(Staff, Staff.id == OrganizationUnitMember.staff_id)
            .outerjoin(PositionProfile, PositionProfile.id == Staff.position_id)
            .where(OrganizationUnit.hospital_code == resolved_hospital_code)
            .order_by(OrganizationUnit.sort_order.asc(), Staff.name.asc())
        )
    ).all()
    memberships = [
        OrganizationUnitMemberOut(
            unit_id=membership.unit_id,
            staff_id=staff.id,
            staff_name=staff.name,
            external_account=staff.external_account,
            position_name=position_name,
            hospital_code=staff.hospital_code,
            hospital_short_name=staff.hospital_short_name,
            is_primary=membership.is_primary,
            is_active=staff.is_active,
            created_at=membership.created_at,
        )
        for membership, staff, position_name in membership_rows
    ]
    member_counts = _build_subtree_member_counts(units, membership_rows)

    manager_staff = Staff.__table__.alias("manager_staff")
    subordinate_staff = Staff.__table__.alias("subordinate_staff")
    relation_rows = (
        await db.execute(
            select(
                StaffManagementRelation,
                manager_staff.c.name.label("manager_name"),
                manager_staff.c.hospital_code.label("manager_hospital_code"),
                manager_staff.c.permission_role.label("manager_permission_role"),
                subordinate_staff.c.name.label("subordinate_name"),
                subordinate_staff.c.hospital_code.label("subordinate_hospital_code"),
                subordinate_staff.c.permission_role.label("subordinate_permission_role"),
            )
            .join(manager_staff, manager_staff.c.id == StaffManagementRelation.manager_staff_id)
            .join(subordinate_staff, subordinate_staff.c.id == StaffManagementRelation.subordinate_staff_id)
            .where(StaffManagementRelation.hospital_code == resolved_hospital_code)
            .order_by(manager_staff.c.name.asc(), subordinate_staff.c.name.asc())
        )
    ).all()
    relations = [
        StaffManagementRelationOut(
            id=relation.id,
            hospital_code=relation.hospital_code,
            manager_staff_id=relation.manager_staff_id,
            manager_name=manager_name,
            subordinate_staff_id=relation.subordinate_staff_id,
            subordinate_name=subordinate_name,
            created_at=relation.created_at,
        )
        for (
            relation,
            manager_name,
            manager_hospital_code,
            manager_permission_role,
            subordinate_name,
            subordinate_hospital_code,
            subordinate_permission_role,
        ) in relation_rows
        if _can_view_management_relation(
            current_user,
            manager_staff_id=relation.manager_staff_id,
            manager_hospital_code=manager_hospital_code,
            manager_permission_role=manager_permission_role,
            subordinate_staff_id=relation.subordinate_staff_id,
            subordinate_hospital_code=subordinate_hospital_code,
            subordinate_permission_role=subordinate_permission_role,
        )
    ]

    return OrganizationOverviewOut(
        hospital_code=resolved_hospital_code,
        hospital_name=hospital_name,
        staff=staff_items,
        units=[
            _to_unit_out(unit, unit_map=unit_map, member_counts=member_counts)
            for unit in units
        ],
        memberships=memberships,
        management_relations=relations,
    )


@router.post("/units", response_model=OrganizationUnitOut, status_code=201)
async def create_organization_unit(
    body: OrganizationUnitCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    name = _clean_text(body.name)
    if not name:
        raise HTTPException(400, "组织名称不能为空")
    hospital_code, hospital_name = await _resolve_request_hospital(db, current_user, body.hospital_code)
    parent_id = await _validate_parent(db, hospital_code=hospital_code, parent_id=body.parent_id)
    await _assert_unique_unit_name(db, hospital_code=hospital_code, parent_id=parent_id, name=name)

    unit = OrganizationUnit(
        hospital_code=hospital_code,
        hospital_name=hospital_name,
        name=name,
        parent_id=parent_id,
        sort_order=body.sort_order,
        is_active=body.is_active,
    )
    db.add(unit)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(400, "组织创建失败，请检查名称是否重复") from exc
    await db.refresh(unit)

    await append_audit_log(
        db,
        operator_name=_operator_name(current_user),
        ip_address=_client_ip(request),
        module_name="组织架构",
        action_name="新增组织",
        content=f"新增组织：{hospital_code} / {name}",
    )
    units = await _load_units(db, hospital_code)
    unit_map = {item.id: item for item in units}
    return _to_unit_out(unit, unit_map=unit_map, member_counts={})


@router.put("/units/{unit_id}", response_model=OrganizationUnitOut)
async def update_organization_unit(
    unit_id: str,
    body: OrganizationUnitUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    unit = await _get_unit_or_404(db, unit_id, current_user)
    updates = body.model_dump(exclude_unset=True)
    next_name = _clean_text(updates.get("name", unit.name))
    if not next_name:
        raise HTTPException(400, "组织名称不能为空")
    next_parent_id = await _validate_parent(
        db,
        hospital_code=unit.hospital_code,
        parent_id=updates.get("parent_id", unit.parent_id),
        current_unit_id=unit.id,
    )
    await _assert_unique_unit_name(
        db,
        hospital_code=unit.hospital_code,
        parent_id=next_parent_id,
        name=next_name,
        exclude_unit_id=unit.id,
    )

    unit.name = next_name
    unit.parent_id = next_parent_id
    if "sort_order" in updates and updates["sort_order"] is not None:
        unit.sort_order = int(updates["sort_order"])
    if "is_active" in updates and updates["is_active"] is not None:
        unit.is_active = bool(updates["is_active"])
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(400, "组织更新失败，请检查名称是否重复") from exc
    await db.refresh(unit)

    await append_audit_log(
        db,
        operator_name=_operator_name(current_user),
        ip_address=_client_ip(request),
        module_name="组织架构",
        action_name="更新组织",
        content=f"更新组织：{unit.hospital_code} / {unit.name}",
    )
    units = await _load_units(db, unit.hospital_code)
    unit_map = {item.id: item for item in units}
    member_counts = {
        unit_id: int(count or 0)
        for unit_id, count in (
            await db.execute(
                select(OrganizationUnitMember.unit_id, func.count(OrganizationUnitMember.id))
                .where(OrganizationUnitMember.unit_id == unit.id)
                .group_by(OrganizationUnitMember.unit_id)
            )
        ).all()
    }
    return _to_unit_out(unit, unit_map=unit_map, member_counts=member_counts)


@router.delete("/units/{unit_id}", status_code=204)
async def delete_organization_unit(
    unit_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    unit = await _get_unit_or_404(db, unit_id, current_user)
    has_children = (
        await db.execute(select(OrganizationUnit.id).where(OrganizationUnit.parent_id == unit.id).limit(1))
    ).scalar_one_or_none()
    if has_children:
        raise HTTPException(400, "请先删除或移动下级组织")
    has_members = (
        await db.execute(select(OrganizationUnitMember.id).where(OrganizationUnitMember.unit_id == unit.id).limit(1))
    ).scalar_one_or_none()
    if has_members:
        raise HTTPException(400, "请先移出该组织下的人员")

    content = f"删除组织：{unit.hospital_code} / {unit.name}"
    await db.delete(unit)
    await db.commit()
    await append_audit_log(
        db,
        operator_name=_operator_name(current_user),
        ip_address=_client_ip(request),
        module_name="组织架构",
        action_name="删除组织",
        content=content,
    )


@router.put("/units/{unit_id}/members", response_model=list[OrganizationUnitMemberOut])
async def replace_organization_unit_members(
    unit_id: str,
    body: OrganizationUnitMemberUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    unit = await _get_unit_or_404(db, unit_id, current_user)
    staff_map = await _load_staff_map(db, hospital_code=unit.hospital_code, staff_ids=body.staff_ids)
    staff_ids = list(staff_map.keys())
    await db.execute(delete(OrganizationUnitMember).where(OrganizationUnitMember.unit_id == unit.id))
    for staff_id in staff_ids:
        db.add(OrganizationUnitMember(unit_id=unit.id, staff_id=staff_id))
    await db.commit()

    await append_audit_log(
        db,
        operator_name=_operator_name(current_user),
        ip_address=_client_ip(request),
        module_name="组织架构",
        action_name="配置组织成员",
        content=f"配置组织成员：{unit.hospital_code} / {unit.name}，成员 {len(staff_ids)} 人",
    )

    rows = (
        await db.execute(
            select(OrganizationUnitMember, Staff, PositionProfile.name)
            .join(Staff, Staff.id == OrganizationUnitMember.staff_id)
            .outerjoin(PositionProfile, PositionProfile.id == Staff.position_id)
            .where(OrganizationUnitMember.unit_id == unit.id)
            .order_by(Staff.name.asc())
        )
    ).all()
    return [
        OrganizationUnitMemberOut(
            unit_id=membership.unit_id,
            staff_id=staff.id,
            staff_name=staff.name,
            external_account=staff.external_account,
            position_name=position_name,
            hospital_code=staff.hospital_code,
            hospital_short_name=staff.hospital_short_name,
            is_primary=membership.is_primary,
            is_active=staff.is_active,
            created_at=membership.created_at,
        )
        for membership, staff, position_name in rows
    ]


@router.post("/units/{unit_id}/members/move", response_model=list[OrganizationUnitMemberOut])
async def move_organization_unit_members(
    unit_id: str,
    body: OrganizationUnitMemberMove,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    source_unit = await _get_unit_or_404(db, unit_id, current_user)
    target_unit_id = _clean_text(body.target_unit_id)
    if not target_unit_id:
        raise HTTPException(400, "请选择目标组织")
    if target_unit_id == source_unit.id:
        raise HTTPException(400, "目标组织不能与当前组织相同")

    target_unit = await db.get(OrganizationUnit, target_unit_id)
    if target_unit is None or target_unit.hospital_code != source_unit.hospital_code:
        raise HTTPException(400, "目标组织不存在或不属于当前机构")
    _assert_unit_access(current_user, target_unit)

    staff_map = await _load_staff_map(db, hospital_code=source_unit.hospital_code, staff_ids=body.staff_ids)
    staff_ids = list(staff_map.keys())
    if not staff_ids:
        raise HTTPException(400, "请选择要移动的成员")

    source_member_ids = set(
        (
            await db.execute(
                select(OrganizationUnitMember.staff_id).where(
                    OrganizationUnitMember.unit_id == source_unit.id,
                    OrganizationUnitMember.staff_id.in_(staff_ids),
                )
            )
        ).scalars().all()
    )
    missing_source_ids = [staff_id for staff_id in staff_ids if staff_id not in source_member_ids]
    if missing_source_ids:
        raise HTTPException(400, "所选人员不在当前组织")

    target_member_ids = set(
        (
            await db.execute(
                select(OrganizationUnitMember.staff_id).where(
                    OrganizationUnitMember.unit_id == target_unit.id,
                    OrganizationUnitMember.staff_id.in_(staff_ids),
                )
            )
        ).scalars().all()
    )

    await db.execute(
        delete(OrganizationUnitMember).where(
            OrganizationUnitMember.unit_id == source_unit.id,
            OrganizationUnitMember.staff_id.in_(staff_ids),
        )
    )
    for staff_id in staff_ids:
        if staff_id not in target_member_ids:
            db.add(OrganizationUnitMember(unit_id=target_unit.id, staff_id=staff_id))
    await db.commit()

    moved_names = "、".join(staff_map[staff_id].name for staff_id in staff_ids)
    await append_audit_log(
        db,
        operator_name=_operator_name(current_user),
        ip_address=_client_ip(request),
        module_name="组织架构",
        action_name="移动组织成员",
        content=(
            f"移动组织成员：{source_unit.hospital_code} / {source_unit.name} -> "
            f"{target_unit.name}，成员 {len(staff_ids)} 人：{moved_names}"
        ),
    )

    rows = (
        await db.execute(
            select(OrganizationUnitMember, Staff, PositionProfile.name)
            .join(Staff, Staff.id == OrganizationUnitMember.staff_id)
            .outerjoin(PositionProfile, PositionProfile.id == Staff.position_id)
            .where(OrganizationUnitMember.unit_id == target_unit.id)
            .order_by(Staff.name.asc())
        )
    ).all()
    return [
        OrganizationUnitMemberOut(
            unit_id=membership.unit_id,
            staff_id=staff.id,
            staff_name=staff.name,
            external_account=staff.external_account,
            position_name=position_name,
            hospital_code=staff.hospital_code,
            hospital_short_name=staff.hospital_short_name,
            is_primary=membership.is_primary,
            is_active=staff.is_active,
            created_at=membership.created_at,
        )
        for membership, staff, position_name in rows
    ]


@router.post("/management-relations", response_model=StaffManagementRelationOut, status_code=201)
async def create_management_relation(
    body: StaffManagementRelationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manager_id = _clean_text(body.manager_staff_id)
    subordinate_id = _clean_text(body.subordinate_staff_id)
    if not manager_id or not subordinate_id:
        raise HTTPException(400, "请选择管理人和被管理人")

    manager = await db.get(Staff, manager_id)
    subordinate = await db.get(Staff, subordinate_id)
    if manager is None or subordinate is None:
        raise HTTPException(400, "所选人员不存在")
    if not manager.hospital_code or manager.hospital_code != subordinate.hospital_code:
        raise HTTPException(400, "管理人和被管理人必须属于同一机构")
    if not is_global_role(current_user.role) and manager.hospital_code != current_user.hospital_code:
        raise HTTPException(403, "只能配置本机构的管理关系")
    _assert_relation_manager_access(current_user, manager)
    if not _can_configure_relation_target(current_user, subordinate):
        raise HTTPException(403, "无权将更高权限人员加入管理范围")

    if await _would_create_management_cycle(
        db,
        hospital_code=manager.hospital_code,
        manager_staff_id=manager.id,
        subordinate_staff_id=subordinate.id,
    ):
        raise HTTPException(400, "该管理关系会形成循环")

    relation = StaffManagementRelation(
        hospital_code=manager.hospital_code,
        manager_staff_id=manager.id,
        subordinate_staff_id=subordinate.id,
    )
    db.add(relation)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(400, "该管理关系已存在") from exc
    await db.refresh(relation)

    await append_audit_log(
        db,
        operator_name=_operator_name(current_user),
        ip_address=_client_ip(request),
        module_name="组织架构",
        action_name="新增管理关系",
        content=f"新增管理关系：{manager.name} 管理 {subordinate.name}",
    )
    return StaffManagementRelationOut(
        id=relation.id,
        hospital_code=relation.hospital_code,
        manager_staff_id=manager.id,
        manager_name=manager.name,
        subordinate_staff_id=subordinate.id,
        subordinate_name=subordinate.name,
        created_at=relation.created_at,
    )


@router.post("/management-relations/bulk", response_model=list[StaffManagementRelationOut], status_code=201)
async def create_management_relations_bulk(
    body: StaffManagementRelationBulkCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manager_id = _clean_text(body.manager_staff_id)
    if not manager_id:
        raise HTTPException(400, "请选择管理人")

    manager = await db.get(Staff, manager_id)
    if manager is None:
        raise HTTPException(400, "管理人不存在")
    if not manager.hospital_code:
        raise HTTPException(400, "管理人未归属机构")
    if not is_global_role(current_user.role) and manager.hospital_code != current_user.hospital_code:
        raise HTTPException(403, "只能配置本机构的管理关系")
    _assert_relation_manager_access(current_user, manager)

    created_relations, target_staff_map = await _create_management_relations_for_staff_ids(
        db,
        hospital_code=manager.hospital_code,
        manager=manager,
        subordinate_staff_ids=body.subordinate_staff_ids,
        current_user=current_user,
    )
    if not created_relations:
        return []

    created_staff_ids = [relation.subordinate_staff_id for relation in created_relations]
    created_names = "、".join(target_staff_map[staff_id].name for staff_id in created_staff_ids)
    await append_audit_log(
        db,
        operator_name=_operator_name(current_user),
        ip_address=_client_ip(request),
        module_name="组织架构",
        action_name="批量新增管理关系",
        content=f"批量新增管理关系：{manager.name} 管理 {len(created_staff_ids)} 人：{created_names}",
    )
    return [
        StaffManagementRelationOut(
            id=relation.id,
            hospital_code=relation.hospital_code,
            manager_staff_id=manager.id,
            manager_name=manager.name,
            subordinate_staff_id=relation.subordinate_staff_id,
            subordinate_name=target_staff_map[relation.subordinate_staff_id].name,
            created_at=relation.created_at,
        )
        for relation in created_relations
    ]


@router.post("/management-relations/by-unit", response_model=list[StaffManagementRelationOut], status_code=201)
async def create_management_relations_by_unit(
    body: StaffManagementRelationByUnitCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manager_id = _clean_text(body.manager_staff_id)
    unit_id = _clean_text(body.unit_id)
    if not manager_id or not unit_id:
        raise HTTPException(400, "请选择管理人和组织")

    manager = await db.get(Staff, manager_id)
    unit = await db.get(OrganizationUnit, unit_id)
    if manager is None:
        raise HTTPException(400, "管理人不存在")
    if unit is None:
        raise HTTPException(400, "组织不存在")
    if not manager.hospital_code or manager.hospital_code != unit.hospital_code:
        raise HTTPException(400, "管理人和组织必须属于同一机构")
    _assert_unit_access(current_user, unit)
    if not is_global_role(current_user.role) and manager.hospital_code != current_user.hospital_code:
        raise HTTPException(403, "只能配置本机构的管理关系")
    _assert_relation_manager_access(current_user, manager)

    units = await _load_units(db, unit.hospital_code)
    unit_map = {item.id: item for item in units}
    target_unit_ids = _collect_unit_subtree_ids(units, unit.id, include_self=True) if body.include_descendants else [unit.id]
    staff_rows = (
        await db.execute(
            select(Staff)
            .join(OrganizationUnitMember, OrganizationUnitMember.staff_id == Staff.id)
            .where(
                OrganizationUnitMember.unit_id.in_(target_unit_ids),
                Staff.hospital_code == unit.hospital_code,
                Staff.is_active.is_(True),
                Staff.id != manager.id,
            )
            .order_by(Staff.name.asc(), Staff.external_account.asc())
        )
    ).scalars().all()
    target_staff_map = {staff.id: staff for staff in staff_rows}
    if not target_staff_map:
        raise HTTPException(400, "所选组织没有可添加的员工")

    target_staff_ids = list(target_staff_map.keys())
    created_relations, created_target_staff_map = await _create_management_relations_for_staff_ids(
        db,
        hospital_code=unit.hospital_code,
        manager=manager,
        subordinate_staff_ids=target_staff_ids,
        current_user=current_user,
    )
    if not created_relations:
        return []
    target_staff_map.update(created_target_staff_map)
    created_staff_ids = [relation.subordinate_staff_id for relation in created_relations]

    created_names = "、".join(target_staff_map[staff_id].name for staff_id in created_staff_ids)
    await append_audit_log(
        db,
        operator_name=_operator_name(current_user),
        ip_address=_client_ip(request),
        module_name="组织架构",
        action_name="按组织新增管理关系",
        content=(
            f"按组织新增管理关系：{manager.name} 管理 "
            f"{_unit_path(unit, unit_map)}"
            f"{'及下级组织' if body.include_descendants else ''}，新增 {len(created_staff_ids)} 人：{created_names}"
        ),
    )

    return [
        StaffManagementRelationOut(
            id=relation.id,
            hospital_code=relation.hospital_code,
            manager_staff_id=manager.id,
            manager_name=manager.name,
            subordinate_staff_id=relation.subordinate_staff_id,
            subordinate_name=target_staff_map[relation.subordinate_staff_id].name,
            created_at=relation.created_at,
        )
        for relation in created_relations
    ]


@router.put("/management-relations/managers/{manager_staff_id}", response_model=list[StaffManagementRelationOut])
async def sync_management_relations_for_manager(
    manager_staff_id: str,
    body: StaffManagementRelationSync,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manager_id = _clean_text(manager_staff_id)
    if not manager_id:
        raise HTTPException(400, "请选择管理人")

    manager = await db.get(Staff, manager_id)
    if manager is None:
        raise HTTPException(400, "管理人不存在")
    if not manager.hospital_code:
        raise HTTPException(400, "管理人未归属机构")
    if not is_global_role(current_user.role) and manager.hospital_code != current_user.hospital_code:
        raise HTTPException(403, "只能配置本机构的管理关系")
    _assert_relation_manager_access(current_user, manager)

    requested_staff_ids = list(dict.fromkeys([*body.subordinate_staff_ids, manager.id]))
    staff_map = await _load_staff_map(
        db,
        hospital_code=manager.hospital_code,
        staff_ids=requested_staff_ids,
    )
    target_staff_map = _filter_relation_target_staff_map(current_user, manager=manager, staff_map=staff_map)
    target_staff_ids = set(target_staff_map.keys())

    existing_relations = (
        await db.execute(
            select(StaffManagementRelation).where(StaffManagementRelation.manager_staff_id == manager.id)
        )
    ).scalars().all()
    existing_staff_ids = {relation.subordinate_staff_id for relation in existing_relations}
    existing_staff_map = await _load_staff_map(
        db,
        hospital_code=manager.hospital_code,
        staff_ids=list(existing_staff_ids),
    )
    configurable_existing_staff_ids = {
        staff_id
        for staff_id in existing_staff_ids
        if staff_id == manager.id
        or (
            (staff := existing_staff_map.get(staff_id)) is not None
            and staff.is_active
            and _can_configure_relation_target(current_user, staff)
        )
    }
    create_staff_ids = sorted(target_staff_ids - existing_staff_ids)
    delete_staff_ids = configurable_existing_staff_ids - target_staff_ids

    for subordinate_id in create_staff_ids:
        if await _would_create_management_cycle(
            db,
            hospital_code=manager.hospital_code,
            manager_staff_id=manager.id,
            subordinate_staff_id=subordinate_id,
        ):
            raise HTTPException(400, f"该管理关系会形成循环：{manager.name} 管理 {target_staff_map[subordinate_id].name}")

    if delete_staff_ids:
        await db.execute(
            delete(StaffManagementRelation).where(
                StaffManagementRelation.manager_staff_id == manager.id,
                StaffManagementRelation.subordinate_staff_id.in_(delete_staff_ids),
            )
        )
    for subordinate_id in create_staff_ids:
        db.add(
            StaffManagementRelation(
                hospital_code=manager.hospital_code,
                manager_staff_id=manager.id,
                subordinate_staff_id=subordinate_id,
            )
        )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(400, "管理关系保存失败，请刷新后重试") from exc

    subordinate_staff = Staff.__table__.alias("subordinate_staff")
    relation_rows = (
        await db.execute(
            select(
                StaffManagementRelation,
                subordinate_staff.c.name.label("subordinate_name"),
                subordinate_staff.c.hospital_code.label("subordinate_hospital_code"),
                subordinate_staff.c.permission_role.label("subordinate_permission_role"),
            )
            .join(subordinate_staff, subordinate_staff.c.id == StaffManagementRelation.subordinate_staff_id)
            .where(StaffManagementRelation.manager_staff_id == manager.id)
            .order_by(subordinate_staff.c.name.asc())
        )
    ).all()
    result = [
        StaffManagementRelationOut(
            id=relation.id,
            hospital_code=relation.hospital_code,
            manager_staff_id=manager.id,
            manager_name=manager.name,
            subordinate_staff_id=relation.subordinate_staff_id,
            subordinate_name=subordinate_name,
            created_at=relation.created_at,
        )
        for relation, subordinate_name, subordinate_hospital_code, subordinate_permission_role in relation_rows
        if _can_view_management_relation(
            current_user,
            manager_staff_id=manager.id,
            manager_hospital_code=manager.hospital_code,
            manager_permission_role=manager.permission_role,
            subordinate_staff_id=relation.subordinate_staff_id,
            subordinate_hospital_code=subordinate_hospital_code,
            subordinate_permission_role=subordinate_permission_role,
        )
    ]

    await append_audit_log(
        db,
        operator_name=_operator_name(current_user),
        ip_address=_client_ip(request),
        module_name="组织架构",
        action_name="同步管理关系",
        content=(
            f"同步管理关系：{manager.name}，可管理 {len(result)} 人，"
            f"新增 {len(create_staff_ids)} 人，移除 {len(delete_staff_ids)} 人"
        ),
    )
    return result


@router.delete("/management-relations/{relation_id}", status_code=204)
async def delete_management_relation(
    relation_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    relation = await db.get(StaffManagementRelation, relation_id)
    if relation is None:
        raise HTTPException(404, "管理关系不存在")
    if not is_global_role(current_user.role) and relation.hospital_code != current_user.hospital_code:
        raise HTTPException(404, "管理关系不存在")
    if relation.manager_staff_id == relation.subordinate_staff_id:
        raise HTTPException(400, "默认管理自己不能取消")
    manager = await db.get(Staff, relation.manager_staff_id)
    if manager is None:
        raise HTTPException(404, "管理关系不存在")
    _assert_relation_manager_access(current_user, manager)
    subordinate = await db.get(Staff, relation.subordinate_staff_id)
    if subordinate is None or not _can_configure_relation_target(current_user, subordinate):
        raise HTTPException(403, "无权删除该人员的管理关系")

    staff_rows = (
        await db.execute(
            select(Staff.id, Staff.name).where(
                or_(
                    Staff.id == relation.manager_staff_id,
                    Staff.id == relation.subordinate_staff_id,
                )
            )
        )
    ).all()
    name_map = {staff_id: name for staff_id, name in staff_rows}
    content = (
        f"删除管理关系：{name_map.get(relation.manager_staff_id, relation.manager_staff_id)} "
        f"管理 {name_map.get(relation.subordinate_staff_id, relation.subordinate_staff_id)}"
    )
    await db.delete(relation)
    await db.commit()
    await append_audit_log(
        db,
        operator_name=_operator_name(current_user),
        ip_address=_client_ip(request),
        module_name="组织架构",
        action_name="删除管理关系",
        content=content,
    )
