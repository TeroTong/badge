from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


HotwordLibraryScope = Literal["personal", "public"]


class HotwordCreate(BaseModel):
    word: str = Field(min_length=1, max_length=200)
    weight: int = Field(default=10, ge=1, le=100)
    is_active: bool = True


class HotwordUpdate(BaseModel):
    word: str | None = Field(default=None, min_length=1, max_length=200)
    weight: int | None = Field(default=None, ge=1, le=100)
    is_active: bool | None = None


class HotwordOut(BaseModel):
    id: str
    group_id: str
    word: str
    weight: int
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class HotwordGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    group_type: str = Field(min_length=1, max_length=50)
    library_scope: HotwordLibraryScope = "public"
    source_label: str = Field(default="行业", min_length=1, max_length=100)


class HotwordGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    group_type: str | None = Field(default=None, min_length=1, max_length=50)
    library_scope: HotwordLibraryScope | None = None
    source_label: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None


class HotwordGroupOut(BaseModel):
    id: str
    name: str
    group_type: str
    library_scope: HotwordLibraryScope
    source_label: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    words: list[HotwordOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}
