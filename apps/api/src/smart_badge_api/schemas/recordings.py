from typing import Any, Literal

from pydantic import BaseModel, model_validator


class RecordingCreate(BaseModel):
    visit_id: str | None = None
    staff_id: str | None = None
    device_id: str | None = None


VALID_RECORDING_STATUSES = {"uploaded", "transcribing", "transcribed", "analyzing", "analyzed", "failed", "filtered"}


class RecordingUpdate(BaseModel):
    visit_id: str | None = None
    linked_visit_ids: list[str] | None = None
    staff_id: str | None = None
    device_id: str | None = None
    status: str | None = None
    duration_seconds: int | None = None

    @model_validator(mode="after")
    def _validate_status(self):
        if self.status is not None and self.status not in VALID_RECORDING_STATUSES:
            raise ValueError(f"status must be one of {VALID_RECORDING_STATUSES}")
        return self


class RecordingOut(BaseModel):
    class LinkedVisitOut(BaseModel):
        id: str
        external_visit_order_no: str | None = None
        external_visit_order_seg: str | None = None
        customer_name: str | None = None
        is_primary: bool = False

    id: str
    visit_id: str | None
    linked_visit_ids: list[str] = []
    linked_visits: list[LinkedVisitOut] = []
    visit_status: str | None = None
    staff_id: str | None
    staff_name: str | None = None
    staff_badge_id: str | None = None
    staff_role: str | None = None
    customer_name: str | None = None
    device_id: str | None
    device_code: str | None = None
    file_name: str
    file_size: int | None
    duration_seconds: int | None
    status: str
    split_parent_recording_id: str | None = None
    split_part_index: int | None = None
    split_at_ms: int | None = None
    has_transcript: bool = False
    created_at: str

    model_config = {"from_attributes": True}


class RecordingSplitRequest(BaseModel):
    split_at_seconds: float | None = None
    split_at_ms: int | None = None
    confirm: bool = False

    @model_validator(mode="after")
    def _validate_split_time(self):
        if self.split_at_seconds is None and self.split_at_ms is None:
            raise ValueError("split_at_seconds or split_at_ms is required")
        if self.split_at_seconds is not None and self.split_at_seconds <= 0:
            raise ValueError("split_at_seconds must be greater than 0")
        if self.split_at_ms is not None and self.split_at_ms <= 0:
            raise ValueError("split_at_ms must be greater than 0")
        return self

    def resolved_split_at_ms(self) -> int:
        if self.split_at_ms is not None:
            return int(self.split_at_ms)
        return int(round(float(self.split_at_seconds or 0) * 1000))


class RecordingSplitPartOut(BaseModel):
    part_index: int
    archive_item_id: str | None = None
    recording: RecordingOut


class RecordingSplitOut(BaseModel):
    original_recording_id: str
    split_at_ms: int
    parts: list[RecordingSplitPartOut]
    message: str


class ArchiveRecordingOut(BaseModel):
    id: str
    stage_key: str | None = None
    sn: str | None = None
    device_code: str | None = None
    file_id: str
    display_file_name: str
    archive_file_name: str | None = None
    staged_file_name: str | None = None
    remote_file_name: str | None = None
    audio_path: str | None = None
    archive_audio_path: str | None = None
    stage_audio_path: str | None = None
    duration_ms: int | None = None
    duration_seconds: int | None = None
    file_size: int | None = None
    create_time: str | None = None
    downloaded_at: str | None = None
    updated_at: str | None = None
    staff_id: str | None = None
    staff_name: str | None = None
    staff_role: str | None = None
    pipeline_status: str | None = None
    quality_stage: str | None = None
    quality_reason: str | None = None
    error_message: str | None = None
    recording_id: str | None = None
    is_split_hidden: bool = False
    visit_id: str | None = None
    linked_visit_ids: list[str] = []
    linked_visit_order_refs: list[str] = []
    linked_customer_names: list[str] = []
    has_visit_link: bool = False
    needs_visit_link: bool = False
    utterance_count: int | None = None
    full_text_length: int | None = None
    has_transcript: bool
    has_analysis: bool
    analysis_summary: dict[str, Any] | None = None


class ArchiveRecordingDateSummaryOut(BaseModel):
    date: str | None = None
    total: int = 0
    linked_count: int = 0
    needs_link_count: int = 0


class ArchiveRecordingPageOut(BaseModel):
    items: list[ArchiveRecordingOut]
    total: int
    page: int
    page_size: int
    pages: int
    date_summaries: list[ArchiveRecordingDateSummaryOut] = []


class ArchiveRecordingDetailOut(ArchiveRecordingOut):
    manifest: dict[str, Any] | None = None
    archive_metadata: dict[str, Any] | None = None
    transcript: dict[str, Any] | None = None
    analysis_result: dict[str, Any] | None = None
    analysis_summary: dict[str, Any] | None = None


class ArchiveRecordingEnsureOut(BaseModel):
    item_id: str
    recording_id: str
    file_name: str
    display_file_name: str
    created_new_recording: bool
    visit_id: str | None = None
    linked_visit_ids: list[str] = []
    linked_visit_order_refs: list[str] = []
    linked_customer_names: list[str] = []


class RecordingMediaSourceOut(BaseModel):
    url: str
    file_name: str
    media_type: str | None = None


class PendingArchiveRecordingOut(BaseModel):
    id: str
    display_file_name: str
    create_time: str | None = None
    duration_seconds: int | None = None
    staff_id: str | None = None
    staff_name: str | None = None
    device_code: str | None = None
    pipeline_status: str | None = None
    recording_id: str | None = None
    has_transcript: bool = False
    has_analysis: bool = False
    match_score: float = 0.0
    match_reasons: list[str] = []


class SapConsultationPayload(BaseModel):
    text: str
    user: str
    zxxx: dict[str, str]
    TAB_SYZ: list[dict[str, str]]


class SapPushTargetOut(BaseModel):
    visit_id: str | None = None
    visit_order_no: str
    visit_order_seg: str | None = None
    customer_name: str
    customer_code: str
    advisor_name: str
    indication_count: int
    recording_count: int = 1
    is_primary: bool = False


class SapPushPreviewOut(BaseModel):
    recording_id: str
    visit_order_no: str
    visit_order_seg: str | None = None
    customer_name: str
    customer_code: str
    advisor_name: str
    indication_count: int
    recording_count: int = 1
    target_count: int = 1
    targets: list[SapPushTargetOut] = []
    payloads: list[SapConsultationPayload]


class RecordingCustomerSegmentOut(BaseModel):
    id: str
    segment_index: int
    label: str
    begin_ms: int
    end_ms: int
    summary: str
    utterance_count: int
    status: str
    mapped_visit_id: str | None = None


class RecordingVisitAnalysisOut(BaseModel):
    id: str
    recording_id: str
    visit_id: str
    visit_order_no: str | None = None
    visit_order_seg: str | None = None
    customer_name: str | None = None
    customer_code: str | None = None
    customer_segment_id: str | None = None
    mapping_status: str
    analysis_status: str
    analysis_task_id: str | None = None
    analysis_error: str | None = None
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    sap_ready_at: str | None = None
    sap_push_log_id: str | None = None


class RecordingMultiCustomerReviewOut(BaseModel):
    recording_id: str
    required: bool
    linked_visit_count: int
    status: Literal["not_required", "pending_mapping", "analyzing", "ready", "failed"]
    message: str
    segments: list[RecordingCustomerSegmentOut] = []
    visit_analyses: list[RecordingVisitAnalysisOut] = []


class RecordingMultiCustomerMappingIn(BaseModel):
    visit_id: str
    customer_segment_id: str


class RecordingMultiCustomerConfirmRequest(BaseModel):
    mappings: list[RecordingMultiCustomerMappingIn]


class SapPushDispatchRequest(BaseModel):
    trigger_mode: Literal["manual", "auto_bind", "scheduled"] = "manual"
    async_dispatch: bool = True
    target_visit_id: str | None = None


class SapPushAttemptOut(BaseModel):
    request_index: int
    success: bool
    http_status_code: int | None = None
    gateway_code: int | str | None = None
    business_status: str | None = None
    business_message: str | None = None
    response_body: Any = None


class SapPushLogOut(BaseModel):
    id: str
    recording_id: str | None = None
    recording_file_name: str | None = None
    recording_created_at: str | None = None
    visit_id: str | None = None
    visit_order_no: str | None = None
    visit_order_seg: str | None = None
    customer_name: str | None = None
    customer_code: str | None = None
    advisor_name: str | None = None
    trigger_mode: str
    status: str
    send_enabled: bool
    initiated_by: str | None = None
    request_url: str | None = None
    trace_id: str | None = None
    request_payloads: list[dict[str, Any]] = []
    gateway_requests: list[dict[str, Any]] = []
    response_items: list[SapPushAttemptOut] = []
    http_status_code: int | None = None
    business_status: str | None = None
    business_message: str | None = None
    error_message: str | None = None
    effective_status: str | None = None
    effective_business_status: str | None = None
    effective_reason: str | None = None
    sent_at: str | None = None
    message_success_notified_at: str | None = None
    message_failure_notified_at: str | None = None
    message_notify_error: str | None = None
    created_at: str
    updated_at: str


class SapPushDispatchOut(BaseModel):
    queued: bool
    dispatch_mode: Literal["dramatiq", "background", "eager"]
    send_enabled: bool
    message: str
    log: SapPushLogOut
    logs: list[SapPushLogOut] = []
