from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class QualityCheckpointCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    score_weight: float = 1.0
    sort_order: int = 0


class QualityCheckpointUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    score_weight: float | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class QualityCheckpointOut(BaseModel):
    id: str
    dimension_id: str
    name: str
    description: str
    score_weight: float
    sort_order: int
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class QualityDimensionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    rule_group_id: str | None = None
    weight: float = 1.0
    sort_order: int = 0


class QualityDimensionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    rule_group_id: str | None = None
    weight: float | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class QualityDimensionOut(BaseModel):
    id: str
    name: str
    description: str
    rule_group_id: str | None
    rule_group_name: str | None = None
    rule_group_detail: str | None = None
    weight: float
    sort_order: int
    is_active: bool
    checkpoints: list[QualityCheckpointOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
