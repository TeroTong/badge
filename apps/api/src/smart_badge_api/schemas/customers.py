from datetime import date, datetime

from pydantic import BaseModel

from smart_badge_api.schemas.recordings import PendingArchiveRecordingOut


class CustomerCreate(BaseModel):
    name: str
    gender: str | None = None
    age: int | None = None
    wechat_external_uid: str | None = None
    source: str | None = None
    notes: str | None = None


class CustomerUpdate(BaseModel):
    name: str | None = None
    gender: str | None = None
    age: int | None = None
    wechat_external_uid: str | None = None
    source: str | None = None
    notes: str | None = None
    is_active: bool | None = None


class CustomerOut(BaseModel):
    id: str
    name: str
    gender: str | None
    age: int | None
    wechat_external_uid: str | None
    external_customer_code: str | None = None
    source: str | None
    notes: str | None
    is_active: bool
    visit_count: int = 0
    recording_count: int = 0
    closed_won_count: int = 0
    total_deposit_principal: float | None = None
    customer_type_code: str | None = None
    customer_type_label: str | None = None
    customer_type_institution_code: str | None = None
    last_visit_at: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CustomerDateSummaryOut(BaseModel):
    date: str | None = None
    total: int = 0


class CustomerPageOut(BaseModel):
    items: list[CustomerOut]
    total: int
    page: int
    page_size: int
    pages: int
    date_summaries: list[CustomerDateSummaryOut] = []


class CustomerDetailRecordingEvalDimensionOut(BaseModel):
    name: str
    point_score: float | None = None
    max_score: float = 1.0
    summary: str | None = None


class CustomerDetailRecordingOut(BaseModel):
    id: str
    visit_id: str | None
    file_name: str
    device_id: str | None
    staff_name: str | None = None
    status: str
    duration_seconds: int | None = None
    created_at: datetime
    transcript_id: str | None = None
    transcript_status: str | None = None
    transcript_provider: str | None = None
    transcript_excerpt: str | None = None
    analysis_task_id: str | None = None
    analysis_status: str | None = None
    analysis_overall_score: float | None = None
    analysis_completed_at: str | None = None
    analysis_summary: str | None = None
    analysis_profile_tags: list[str] = []
    analysis_primary_demands: list[str] = []
    analysis_concerns: list[str] = []
    analysis_recommendations: list[str] = []
    analysis_evaluation_dimensions: list[CustomerDetailRecordingEvalDimensionOut] = []


class CustomerDetailVisitOrderLineItemOut(BaseModel):
    fzdh: str | None = None
    advxc_long: str | None = None
    assxc: str | None = None
    fzsj: str | None = None
    fzsta_txt: str | None = None
    jcsta_txt: str | None = None


class CustomerDetailVisitOrderSummaryOut(BaseModel):
    dzdh: str | None = None
    jgbm: str | None = None
    crtdt: str | None = None
    crttm: str | None = None
    dzsta_txt: str | None = None
    dzly_txt: str | None = None
    dymd_txt: str | None = None
    dztyp_txt: str | None = None
    jgks_txt: str | None = None
    fzuer_long: str | None = None
    vipkf: str | None = None
    kulvl_dq: str | None = None
    kutyp_dq_txt: str | None = None
    kut30_dq_txt: str | None = None
    kusta_dq_txt: str | None = None
    remark_dz: str | None = None
    line_items: list[CustomerDetailVisitOrderLineItemOut] = []


class CustomerDetailVisitOut(BaseModel):
    id: str
    status: str
    external_visit_order_no: str | None = None
    visit_date: date | None = None
    visit_time: str | None = None
    consultant_name: str | None = None
    doctor_name: str | None = None
    deal_status: str | None = None
    deposit_principal: float | None = None
    deposit_bonus: float | None = None
    arrival_purpose: str | None = None
    project_needs: str | None = None
    notes: str | None = None
    created_at: datetime
    recordings: list[CustomerDetailRecordingOut]
    pending_archive_recordings: list[PendingArchiveRecordingOut] = []
    sap_consultation_texts: list[str] = []
    visit_order_summary: CustomerDetailVisitOrderSummaryOut | None = None


class CustomerDetailOut(CustomerOut):
    recording_count: int = 0
    transcript_count: int = 0
    analyzed_recording_count: int = 0
    visits: list[CustomerDetailVisitOut]


class CustomerMergedThemeOut(BaseModel):
    label: str
    detail: str | None = None
    count: int = 0
    latest_seen_at: str | None = None


class CustomerMergedDimensionOut(BaseModel):
    name: str
    average_score: float
    latest_score: float | None = None
    mention_count: int = 0
    latest_comment: str | None = None


class CustomerMergedTimelineOut(BaseModel):
    task_id: str
    recording_id: str | None = None
    recording_name: str | None = None
    visit_id: str | None = None
    visit_status: str | None = None
    project_name: str | None = None
    deal_amount: float | None = None
    overall_score: float | None = None
    quality_label: str
    completed_at: str | None = None


class CustomerMergedAnalysisOut(BaseModel):
    customer_id: str
    customer_name: str
    total_visits: int = 0
    total_recordings: int = 0
    analyzed_recordings: int = 0
    average_score: float | None = None
    latest_score: float | None = None
    score_delta: float | None = None
    score_trend: str = "stable"
    merged_summary: str = ""
    latest_task_id: str | None = None
    latest_recording_id: str | None = None
    last_analyzed_at: str | None = None
    recurring_focus_areas: list[CustomerMergedThemeOut] = []
    recurring_concerns: list[CustomerMergedThemeOut] = []
    profile_tags: list[CustomerMergedThemeOut] = []
    dimension_averages: list[CustomerMergedDimensionOut] = []
    timeline: list[CustomerMergedTimelineOut] = []


# ── 标签完成度 ──────────────────────────────────────


class TagExtractionItem(BaseModel):
    category_id: str
    category_name: str
    weight_level: int | None = None
    available_tags: list[str] = []
    extracted_values: list[str] = []
    evidence: str | None = None
    status: str = "not_extracted"  # extracted | not_extracted
    last_seen_at: str | None = None


class TagCompletionOut(BaseModel):
    customer_id: str
    total_categories: int = 0
    extracted_categories: int = 0
    completion_rate: float = 0.0
    categories: list[TagExtractionItem] = []


# ── 到诊单分组 ──────────────────────────────────────


class VisitOrderItemOut(BaseModel):
    dzseg: str | None = None
    jcsta_txt: str | None = None
    remark_dz: str | None = None


class VisitOrderGroupOut(BaseModel):
    dzdh: str
    visit_date: str | None = None
    consultant_name: str | None = None
    status_text: str | None = None
    customer_type: str | None = None
    customer_type_t30: str | None = None
    member_level: str | None = None
    remark: str | None = None
    items: list[VisitOrderItemOut] = []


class CustomerVisitOrdersOut(BaseModel):
    customer_id: str
    customer_code: str | None = None
    total_visits: int = 0
    visit_groups: list[VisitOrderGroupOut] = []
