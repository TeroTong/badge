from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


RiskSeverity = Literal["low", "medium", "high", "critical"]
RiskRecordStatus = Literal["open", "resolved", "ignored"]


class RiskRuleBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    match_type: str = Field(..., min_length=1, max_length=50)
    severity: RiskSeverity = "medium"
    risk_label: str = Field(default="", max_length=100)
    description: str = ""
    match_config: dict[str, Any] = Field(default_factory=dict)
    note: str = ""
    is_active: bool = True


class RiskRuleCreate(RiskRuleBase):
    pass


class RiskRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    match_type: str | None = Field(default=None, min_length=1, max_length=50)
    severity: RiskSeverity | None = None
    risk_label: str | None = Field(default=None, max_length=100)
    description: str | None = None
    match_config: dict[str, Any] | None = None
    note: str | None = None
    is_active: bool | None = None


class RiskRuleOut(RiskRuleBase):
    id: str
    created_at: str
    updated_at: str


class RiskRecordOut(BaseModel):
    id: str
    rule_id: str | None = None
    task_id: str
    recording_id: str | None = None
    recording_name: str | None = None
    visit_id: str | None = None
    staff_id: str | None = None
    staff_name: str | None = None
    staff_badge_id: str | None = None
    customer_id: str | None = None
    customer_name: str | None = None
    source_type: str
    rule_name: str
    risk_label: str
    severity: RiskSeverity
    status: RiskRecordStatus
    matched_dimension_name: str | None = None
    matched_keywords: list[str] = []
    overall_score: float | None = None
    summary: str = ""
    hit_excerpt: str = ""
    created_at: str
    resolved_at: str | None = None


class RiskRecordDetailOut(RiskRecordOut):
    evidence: dict[str, Any] | None = None
    recording_status: str | None = None
    task_status: str | None = None
    task_completed_at: str | None = None


class RiskRecordStatusUpdate(BaseModel):
    status: RiskRecordStatus


class RiskRecordOverviewOut(BaseModel):
    total: int
    open_count: int
    resolved_count: int
    ignored_count: int
    critical_count: int
    high_count: int
    medium_count: int
    low_count: int
