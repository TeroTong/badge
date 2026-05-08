from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.db.default_data import ensure_rule_groups
from smart_badge_api.db.models import QualityDimension, RuleGroup, SummaryTemplate
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.rule_groups import RuleGroupCreate, RuleGroupOut, RuleGroupUpdate

router = APIRouter(prefix="/rule-groups", tags=["规则组管理"])


@router.get("", response_model=list[RuleGroupOut])
async def list_rule_groups(
    keyword: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    await ensure_rule_groups(db)
    stmt = select(RuleGroup)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(RuleGroup.name.ilike(like), RuleGroup.detail.ilike(like), RuleGroup.note.ilike(like)))
    stmt = stmt.order_by(RuleGroup.updated_at.desc(), RuleGroup.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=RuleGroupOut, status_code=status.HTTP_201_CREATED)
async def create_rule_group(body: RuleGroupCreate, db: AsyncSession = Depends(get_db)):
    rule_group = RuleGroup(**body.model_dump())
    db.add(rule_group)
    await db.commit()
    await db.refresh(rule_group)
    return rule_group


@router.put("/{rule_group_id}", response_model=RuleGroupOut)
async def update_rule_group(rule_group_id: str, body: RuleGroupUpdate, db: AsyncSession = Depends(get_db)):
    rule_group = await db.get(RuleGroup, rule_group_id)
    if not rule_group:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "规则组不存在")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(rule_group, key, value)
    await db.commit()
    await db.refresh(rule_group)
    return rule_group


@router.delete("/{rule_group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule_group(rule_group_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    rule_group = await db.get(RuleGroup, rule_group_id)
    if not rule_group:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "规则组不存在")
    await db.execute(
        update(SummaryTemplate).where(SummaryTemplate.rule_group_id == rule_group_id).values(rule_group_id=None)
    )
    await db.execute(
        update(QualityDimension).where(QualityDimension.rule_group_id == rule_group_id).values(rule_group_id=None)
    )
    await db.delete(rule_group)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
