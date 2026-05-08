from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.db.default_data import ensure_quality_dimensions
from smart_badge_api.db.models import QualityCheckpoint, QualityDimension
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.quality import (
    QualityCheckpointCreate,
    QualityCheckpointOut,
    QualityCheckpointUpdate,
    QualityDimensionCreate,
    QualityDimensionOut,
    QualityDimensionUpdate,
)

router = APIRouter(prefix="/quality", tags=["质检配置"])


def _to_dimension_out(dimension: QualityDimension) -> QualityDimensionOut:
    return QualityDimensionOut(
        id=dimension.id,
        name=dimension.name,
        description=dimension.description,
        rule_group_id=dimension.rule_group_id,
        rule_group_name=dimension.rule_group.name if dimension.rule_group else None,
        rule_group_detail=dimension.rule_group.detail if dimension.rule_group else None,
        weight=dimension.weight,
        sort_order=dimension.sort_order,
        is_active=dimension.is_active,
        checkpoints=[QualityCheckpointOut.model_validate(item) for item in dimension.checkpoints],
        created_at=dimension.created_at,
        updated_at=dimension.updated_at,
    )


@router.get("/dimensions", response_model=list[QualityDimensionOut])
async def list_dimensions(db: AsyncSession = Depends(get_db)):
    await ensure_quality_dimensions(db)
    result = await db.execute(
        select(QualityDimension)
        .options(selectinload(QualityDimension.checkpoints), selectinload(QualityDimension.rule_group))
        .order_by(QualityDimension.sort_order, QualityDimension.created_at)
    )
    return [_to_dimension_out(item) for item in result.scalars().all()]


@router.post("/dimensions", response_model=QualityDimensionOut, status_code=status.HTTP_201_CREATED)
async def create_dimension(body: QualityDimensionCreate, db: AsyncSession = Depends(get_db)):
    dimension = QualityDimension(**body.model_dump())
    db.add(dimension)
    await db.commit()
    await db.refresh(dimension, ["checkpoints", "rule_group"])
    return _to_dimension_out(dimension)


@router.put("/dimensions/{dim_id}", response_model=QualityDimensionOut)
async def update_dimension(dim_id: str, body: QualityDimensionUpdate, db: AsyncSession = Depends(get_db)):
    dimension = await db.get(
        QualityDimension,
        dim_id,
        options=[selectinload(QualityDimension.checkpoints), selectinload(QualityDimension.rule_group)],
    )
    if not dimension:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "质检维度不存在")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(dimension, key, value)
    await db.commit()
    await db.refresh(dimension, ["checkpoints", "rule_group"])
    return _to_dimension_out(dimension)


@router.delete("/dimensions/{dim_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dimension(dim_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    dimension = await db.get(QualityDimension, dim_id)
    if not dimension:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "质检维度不存在")
    await db.delete(dimension)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/dimensions/{dim_id}/checkpoints", response_model=QualityCheckpointOut, status_code=status.HTTP_201_CREATED)
async def create_checkpoint(dim_id: str, body: QualityCheckpointCreate, db: AsyncSession = Depends(get_db)):
    dimension = await db.get(QualityDimension, dim_id)
    if not dimension:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "质检维度不存在")
    checkpoint = QualityCheckpoint(dimension_id=dim_id, **body.model_dump())
    db.add(checkpoint)
    await db.commit()
    await db.refresh(checkpoint)
    return QualityCheckpointOut.model_validate(checkpoint)


@router.put("/checkpoints/{cp_id}", response_model=QualityCheckpointOut)
async def update_checkpoint(cp_id: str, body: QualityCheckpointUpdate, db: AsyncSession = Depends(get_db)):
    checkpoint = await db.get(QualityCheckpoint, cp_id)
    if not checkpoint:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "质检点不存在")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(checkpoint, key, value)
    await db.commit()
    await db.refresh(checkpoint)
    return QualityCheckpointOut.model_validate(checkpoint)


@router.delete("/checkpoints/{cp_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_checkpoint(cp_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    checkpoint = await db.get(QualityCheckpoint, cp_id)
    if not checkpoint:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "质检点不存在")
    await db.delete(checkpoint)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
