from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.audit import append_audit_log
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.core.permissions import can_manage_role, normalize_permission_role
from smart_badge_api.db.models import PositionProfile, Staff, User
from smart_badge_api.db.session import get_db
from smart_badge_api.db.system_defaults import ensure_system_positions
from smart_badge_api.schemas.positions import PositionCreate, PositionOut, PositionUpdate

router = APIRouter(prefix="/positions", tags=["岗位管理"])


def _to_out(position: PositionProfile) -> PositionOut:
    return PositionOut(
        id=position.id,
        name=position.name,
        position_type=position.position_type,
        mapped_role=normalize_permission_role(position.mapped_role),
        is_super_admin=position.is_super_admin,
        note=position.note,
        is_active=position.is_active,
        created_at=position.created_at.isoformat() if position.created_at else "",
        updated_at=position.updated_at.isoformat() if position.updated_at else "",
    )


async def _ensure_position_not_in_use(db: AsyncSession, position_id: str) -> None:
    staff_count = (
        await db.execute(select(func.count()).select_from(Staff).where(Staff.position_id == position_id))
    ).scalar_one()
    if staff_count:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该岗位仍被人员引用，无法删除")


def _assert_position_management_access(current_user: User, mapped_role: str) -> None:
    current_role = normalize_permission_role(current_user.role)
    if current_role not in {"super_admin", "system_admin"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "当前角色无权维护岗位定义")
    if not can_manage_role(current_role, mapped_role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权维护该权限级别的岗位")


@router.get("", response_model=list[PositionOut])
async def list_positions(
    keyword: str | None = Query(default=None),
    position_type: str | None = Query(default=None),
    is_super_admin: bool | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await ensure_system_positions(db)
    stmt = select(PositionProfile).order_by(PositionProfile.created_at.asc())
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(PositionProfile.name.ilike(like), PositionProfile.note.ilike(like)))
    if position_type:
        stmt = stmt.where(PositionProfile.position_type == position_type)
    if is_super_admin is not None:
        stmt = stmt.where(PositionProfile.is_super_admin == is_super_admin)
    result = await db.execute(stmt)
    positions = result.scalars().all()
    current_role = normalize_permission_role(current_user.role)
    if current_role == "hospital_admin":
        positions = [
            item
            for item in positions
            if normalize_permission_role(item.mapped_role) in {"staff", "hospital_admin"}
        ]
    elif current_role != "super_admin":
        positions = [item for item in positions if can_manage_role(current_role, item.mapped_role)]
    return [_to_out(item) for item in positions]


@router.post("", response_model=PositionOut, status_code=status.HTTP_201_CREATED)
async def create_position(
    body: PositionCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    payload = body.model_dump()
    payload["mapped_role"] = normalize_permission_role(payload.get("mapped_role"))
    _assert_position_management_access(current_user, payload["mapped_role"])
    position = PositionProfile(**payload)
    db.add(position)
    await db.commit()
    await db.refresh(position)
    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="岗位管理",
        action_name="新增岗位",
        content=f"新增岗位：{position.name}",
    )
    return _to_out(position)


@router.put("/{position_id}", response_model=PositionOut)
async def update_position(
    position_id: str,
    body: PositionUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    position = await db.get(PositionProfile, position_id)
    if not position:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    _assert_position_management_access(current_user, position.mapped_role)
    next_role = normalize_permission_role(body.mapped_role or position.mapped_role)
    _assert_position_management_access(current_user, next_role)
    for key, value in body.model_dump(exclude_unset=True).items():
        if key == "mapped_role" and value is not None:
            value = normalize_permission_role(value)
        setattr(position, key, value)
    await db.commit()
    await db.refresh(position)
    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="岗位管理",
        action_name="更新岗位",
        content=f"更新岗位：{position.name}",
    )
    return _to_out(position)


@router.delete("/{position_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_position(
    position_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    position = await db.get(PositionProfile, position_id)
    if not position:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    _assert_position_management_access(current_user, position.mapped_role)
    await _ensure_position_not_in_use(db, position_id)
    name = position.name
    await db.delete(position)
    await db.commit()
    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="岗位管理",
        action_name="删除岗位",
        content=f"删除岗位：{name}",
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
