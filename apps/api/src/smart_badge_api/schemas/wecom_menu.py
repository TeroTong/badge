from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class WecomMenuEntryOut(BaseModel):
    label: str
    type: str = "view"
    level: int = 1
    target_path: str | None = None
    target_url: str | None = None


class WecomMenuStateOut(BaseModel):
    agent_id: str
    exists: bool = True
    source: str
    menu: dict[str, Any] = Field(default_factory=dict)
    entries: list[WecomMenuEntryOut] = Field(default_factory=list)


class WecomMenuActionOut(BaseModel):
    agent_id: str
    action: str
    menu: dict[str, Any] = Field(default_factory=dict)
    entries: list[WecomMenuEntryOut] = Field(default_factory=list)
