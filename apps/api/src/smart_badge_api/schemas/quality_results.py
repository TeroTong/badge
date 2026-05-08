from __future__ import annotations

from pydantic import BaseModel


class QualityResultDimensionOut(BaseModel):
    name: str
    score: float
    comment: str


class QualityResultOut(BaseModel):
    id: str
    file_name: str
    status: str
    source_type: str
    quality_label: str
    quality_tone: str
    overall_score: float | None = None
    dialogue_type: str | None = None
    focus_areas: list[str] = []
    concern_count: int = 0
    tag_count: int = 0
    dimension_count: int = 0
    recording_id: str | None = None
    recording_name: str | None = None
    recording_status: str | None = None
    visit_id: str | None = None
    staff_id: str | None = None
    staff_name: str | None = None
    staff_badge_id: str | None = None
    customer_id: str | None = None
    customer_name: str | None = None
    recorded_at: str | None = None
    created_at: str
    completed_at: str | None = None


class QualityResultDetailOut(QualityResultOut):
    error_message: str | None = None
    duration_ms: int | None = None
    segment_count: int | None = None
    dimensions: list[QualityResultDimensionOut] = []
    customer_demands: dict | None = None
    customer_concerns: dict | None = None
    customer_profile: dict | None = None
    consultation_evaluation: dict | None = None
