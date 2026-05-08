from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.db.default_data import ensure_summary_templates
from smart_badge_api.db.models import SummaryTemplate
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.templates import (
    SummaryTemplateCreate,
    SummaryTemplateOut,
    SummaryTemplateUpdate,
)

router = APIRouter(prefix="/templates", tags=["总结模板"])
_DISABLED_TEMPLATE_TYPES = {"customer_value"}


def _normalize_template_type(value: str | None) -> str:
    return (value or "").strip().lower()


def _validate_template_type(value: str | None) -> None:
    if _normalize_template_type(value) in _DISABLED_TEMPLATE_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "客户潜力/意向类模板已停用")


def _to_out(template: SummaryTemplate) -> SummaryTemplateOut:
    return SummaryTemplateOut(
        id=template.id,
        name=template.name,
        template_type=template.template_type,
        content=template.content,
        rule_group_id=template.rule_group_id,
        rule_group_name=template.rule_group.name if template.rule_group else None,
        rule_group_detail=template.rule_group.detail if template.rule_group else None,
        is_active=template.is_active,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


@router.get("", response_model=list[SummaryTemplateOut])
async def list_templates(db: AsyncSession = Depends(get_db)):
    await ensure_summary_templates(db)
    result = await db.execute(
        select(SummaryTemplate)
        .options(selectinload(SummaryTemplate.rule_group))
        .order_by(SummaryTemplate.updated_at.desc(), SummaryTemplate.created_at.desc())
    )
    return [
        _to_out(item)
        for item in result.scalars().all()
        if _normalize_template_type(item.template_type) not in _DISABLED_TEMPLATE_TYPES
    ]


@router.post("", response_model=SummaryTemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(body: SummaryTemplateCreate, db: AsyncSession = Depends(get_db)):
    _validate_template_type(body.template_type)
    template = SummaryTemplate(**body.model_dump())
    db.add(template)
    await db.commit()
    await db.refresh(template, ["rule_group"])
    return _to_out(template)


@router.put("/{tpl_id}", response_model=SummaryTemplateOut)
async def update_template(tpl_id: str, body: SummaryTemplateUpdate, db: AsyncSession = Depends(get_db)):
    template = await db.get(SummaryTemplate, tpl_id, options=[selectinload(SummaryTemplate.rule_group)])
    if not template:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    next_type = body.template_type if body.template_type is not None else template.template_type
    _validate_template_type(next_type)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(template, key, value)
    await db.commit()
    await db.refresh(template, ["rule_group"])
    return _to_out(template)


@router.delete("/{tpl_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(tpl_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    template = await db.get(SummaryTemplate, tpl_id)
    if not template:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    await db.delete(template)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
