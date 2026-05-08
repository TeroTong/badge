from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class RuleGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    detail: str = ""
    note: str = ""
    created_by: str = "admin"


class RuleGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    detail: str | None = None
    note: str | None = None
    created_by: str | None = None
    is_active: bool | None = None


class RuleGroupOut(BaseModel):
    id: str
    name: str
    detail: str
    note: str
    created_by: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
