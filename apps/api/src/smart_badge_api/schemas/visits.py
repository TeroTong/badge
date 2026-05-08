from datetime import date

from pydantic import BaseModel, Field

from smart_badge_api.schemas.recordings import PendingArchiveRecordingOut


class VisitCreate(BaseModel):
    customer_id: str
    consultant_id: str | None = None
    doctor_id: str | None = None
    status: str = "created"
    visit_date: date | None = None
    deposit_principal: float | None = None
    deposit_bonus: float | None = None
    notes: str | None = None


class VisitUpdate(BaseModel):
    consultant_id: str | None = None
    doctor_id: str | None = None
    status: str | None = None
    visit_date: date | None = None
    deposit_principal: float | None = None
    deposit_bonus: float | None = None
    notes: str | None = None


class VisitOut(BaseModel):
    id: str
    customer_id: str
    customer_name: str = ""
    customer_code: str | None = None
    customer_source: str | None = None
    consultant_id: str | None
    consultant_name: str | None = None
    doctor_id: str | None
    doctor_name: str | None = None
    status: str
    deal_status: str | None = None
    visit_date: date | None
    visit_time: str | None = None
    deposit_principal: float | None
    deposit_bonus: float | None
    recording_count: int = 0
    customer_type_code: str | None = None
    customer_type_label: str | None = None
    arrival_purpose: str | None = None
    project_needs: str | None = None
    notes: str | None
    created_at: str

    model_config = {"from_attributes": True}


class VisitDateSummaryOut(BaseModel):
    date: str | None = None
    total: int = 0


class VisitPageOut(BaseModel):
    items: list[VisitOut]
    total: int
    page: int
    page_size: int
    pages: int
    date_summaries: list[VisitDateSummaryOut] = []


class VisitDetailRecordingOut(BaseModel):
    id: str
    file_name: str
    is_primary: bool = False
    device_id: str | None = None
    device_code: str | None = None
    staff_name: str | None = None
    staff_badge_id: str | None = None
    status: str
    duration_seconds: int | None = None
    created_at: str
    transcript_id: str | None = None
    transcript_status: str | None = None
    transcript_provider: str | None = None
    transcript_excerpt: str | None = None
    analysis_task_id: str | None = None
    analysis_status: str | None = None
    analysis_overall_score: float | None = None
    analysis_completed_at: str | None = None
    analysis_result: dict | None = None


class VisitOrderLineItemOut(BaseModel):
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


class VisitOrderContextOut(BaseModel):
    jgbm: str | None = None
    customer_type_code: str | None = None
    customer_type_label: str | None = None
    triage_time: str | None = None
    consult_time: str | None = None
    arrival_status: str | None = None
    deal_status_text: str | None = None
    visit_purpose: str | None = None
    consult_project: str | None = None
    demand_remark: str | None = None
    line_items: list[VisitOrderLineItemOut] = Field(default_factory=list)


class VisitDetailOut(VisitOut):
    customer_gender: str | None = None
    customer_age: int | None = None
    customer_wechat_external_uid: str | None = None
    customer_notes: str | None = None
    transcript_count: int = 0
    analyzed_recording_count: int = 0
    latest_recording_id: str | None = None
    latest_transcript_id: str | None = None
    latest_analysis_task_id: str | None = None
    latest_analysis_status: str | None = None
    latest_analysis_overall_score: float | None = None
    latest_analysis_completed_at: str | None = None
    latest_analysis_result: dict | None = None
    latest_transcript_excerpt: str | None = None
    visit_order_context: VisitOrderContextOut | None = None
    recordings: list[VisitDetailRecordingOut]
    pending_archive_recordings: list[PendingArchiveRecordingOut] = []


class CustomerVisitBatchOut(BaseModel):
    customer_id: str
    visits: list[VisitOut] = []
