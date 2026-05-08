from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.db.models import RiskRule
from smart_badge_api.db.risk_defaults import ensure_risk_rule_defaults
from smart_badge_api.db.session import get_db
from smart_badge_api.risk.service import purge_risk_records_for_rule
from smart_badge_api.schemas.risk import RiskRuleCreate, RiskRuleOut, RiskRuleUpdate

router = APIRouter(prefix="/risk-rules", tags=["risk-rules"])


def _to_out(item: RiskRule) -> RiskRuleOut:
    return RiskRuleOut(
        id=item.id,
        name=item.name,
        match_type=item.match_type,
        severity=item.severity,
        risk_label=item.risk_label,
        description=item.description,
        match_config=item.match_config or {},
        note=item.note,
        is_active=item.is_active,
        created_at=item.created_at.isoformat() if item.created_at else "",
        updated_at=item.updated_at.isoformat() if item.updated_at else "",
    )


@router.get("", response_model=list[RiskRuleOut])
async def list_risk_rules(db: AsyncSession = Depends(get_db)):
    await ensure_risk_rule_defaults(db)
    rows = (await db.execute(select(RiskRule).order_by(RiskRule.created_at.asc()))).scalars().all()
    return [_to_out(item) for item in rows]


@router.post("", response_model=RiskRuleOut, status_code=status.HTTP_201_CREATED)
async def create_risk_rule(body: RiskRuleCreate, db: AsyncSession = Depends(get_db)):
    exists = (
        await db.execute(select(RiskRule).where(RiskRule.name == body.name))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Risk rule name already exists")

    item = RiskRule(**body.model_dump())
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _to_out(item)


@router.put("/{rule_id}", response_model=RiskRuleOut)
async def update_risk_rule(rule_id: str, body: RiskRuleUpdate, db: AsyncSession = Depends(get_db)):
    item = await db.get(RiskRule, rule_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Risk rule not found")

    updates = body.model_dump(exclude_unset=True)
    new_name = updates.get("name")
    if new_name and new_name != item.name:
        exists = (
            await db.execute(select(RiskRule).where(RiskRule.name == new_name))
        ).scalar_one_or_none()
        if exists:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Risk rule name already exists")

    for key, value in updates.items():
        setattr(item, key, value)

    await db.commit()
    await db.refresh(item)
    response = _to_out(item)

    await purge_risk_records_for_rule(db, item.id)
    return response


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_risk_rule(rule_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    item = await db.get(RiskRule, rule_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Risk rule not found")

    await purge_risk_records_for_rule(db, item.id)
    await db.delete(item)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
