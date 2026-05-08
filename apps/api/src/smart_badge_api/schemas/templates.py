from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SummaryTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    template_type: str = Field(min_length=1, max_length=50)
    content: str = Field(min_length=1)
    rule_group_id: str | None = None


class SummaryTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    template_type: str | None = Field(default=None, min_length=1, max_length=50)
    content: str | None = Field(default=None, min_length=1)
    rule_group_id: str | None = None
    is_active: bool | None = None


class SummaryTemplateOut(BaseModel):
    id: str
    name: str
    template_type: str
    content: str
    rule_group_id: str | None
    rule_group_name: str | None = None
    rule_group_detail: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
