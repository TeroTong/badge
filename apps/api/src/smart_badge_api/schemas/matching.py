from __future__ import annotations

from pydantic import BaseModel, Field


class MatchEvidenceOut(BaseModel):
    type: str
    label: str
    detail: str
    strength: str = "medium"


class VisitOrderMatchLineItemOut(BaseModel):
    fzdh: str | None = None
    dzseg: str | None = None
    triage_staff_code: str | None = None
    triage_staff_name: str | None = None
    triage_time: str | None = None
    consult_time: str | None = None
    triage_status_text: str | None = None
    deal_status_text: str | None = None
    consult_project: str | None = None
    note_summary: str | None = None


class VisitOrderMatchCandidateOut(BaseModel):
    visit_order_id: str
    local_visit_id: str | None = None
    associated_local_visit_ids: list[str] = Field(default_factory=list)
    companion_visit_order_refs: list[str] = Field(default_factory=list)
    companion_customer_codes: list[str] = Field(default_factory=list)
    dzdh: str
    dzseg: str | None = None
    customer_name: str | None = None
    customer_code: str | None = None
    customer_type_code: str | None = None
    customer_type_label: str | None = None
    visit_date: str | None = None
    advisor_code: str | None = None
    fzuer: str | None = None
    fzuer_long: str | None = None
    triage_time: str | None = None
    confidence: float
    decision: str
    method: str
    reasons: list[str]
    excluded_reasons: list[str] = Field(default_factory=list)
    identity_conflicts: list[str] = Field(default_factory=list)
    manual_review_required: bool = False
    manual_review_reason: str | None = None
    evidence: list[MatchEvidenceOut]
    merged_segments: list[str] = Field(default_factory=list)
    merged_line_items: list[VisitOrderMatchLineItemOut] = Field(default_factory=list)
    linked_recording_count: int = 0
    linked_recording_names: list[str] = Field(default_factory=list)


class RecordingMatchCandidateOut(BaseModel):
    recording_id: str
    local_visit_id: str | None = None
    file_name: str
    created_at: str
    staff_name: str | None = None
    advisor_code: str | None = None
    customer_name: str | None = None
    current_visit_id: str | None = None
    current_visit_order_no: str | None = None
    current_visit_order_seg: str | None = None
    confidence: float
    decision: str
    method: str
    reasons: list[str]
    excluded_reasons: list[str] = Field(default_factory=list)
    identity_conflicts: list[str] = Field(default_factory=list)
    manual_review_required: bool = False
    manual_review_reason: str | None = None
    evidence: list[MatchEvidenceOut]


class RecordingVisitOrderMatchOut(BaseModel):
    recording_id: str
    file_name: str
    record_date: str | None = None
    advisor_code: str | None = None
    customer_code: str | None = None
    customer_name: str | None = None
    linked_visit_id: str | None = None
    linked_visit_ids: list[str] = Field(default_factory=list)
    linked_visit_order_refs: list[str] = Field(default_factory=list)
    linked_visit_order_no: str | None = None
    linked_visit_order_seg: str | None = None
    auto_applied: bool = False
    identity_conflicts: list[str] = Field(default_factory=list)
    manual_review_required: bool = False
    manual_review_reason: str | None = None
    summary: str
    analyzed_at: str
    candidates: list[VisitOrderMatchCandidateOut]


class VisitOrderRecordingMatchOut(BaseModel):
    visit_order_id: str
    local_visit_id: str | None = None
    dzdh: str
    dzseg: str | None = None
    visit_date: str | None = None
    advisor_code: str | None = None
    customer_code: str | None = None
    customer_name: str | None = None
    customer_type_code: str | None = None
    customer_type_label: str | None = None
    linked_recording_ids: list[str]
    identity_conflicts: list[str] = Field(default_factory=list)
    manual_review_required: bool = False
    manual_review_reason: str | None = None
    summary: str
    analyzed_at: str
    candidates: list[RecordingMatchCandidateOut]
