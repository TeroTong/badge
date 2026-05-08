from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AsrUsageRangeOut(BaseModel):
    label: str
    start_date: str
    end_date: str
    request_count: int
    duration_seconds: int


class AsrQuotaPackageOut(BaseModel):
    name: str
    fee_mode: bool
    total_seconds: int
    remaining_seconds: int
    used_seconds: int
    effective_time: str | None = None
    expiry_time: str | None = None
    pid: int | None = None
    unit: str | None = None
    sub_product_code: str | None = None
    available_type: int


class AsrMonitoringOverviewOut(BaseModel):
    provider: str
    has_tencent_credentials: bool
    request_log_available: bool
    cloud_audit_log_available: bool
    quota_state: Literal["normal", "exhausted", "unknown"]
    quota_message: str | None = None
    local_exact_count: int
    local_success_count: int
    local_failed_count: int
    local_submitted_duration_ms: int
    local_recognized_duration_ms: int
    cloud_total_count: int
    cloud_failed_count: int
    latest_event_at: str | None = None
    latest_error_message: str | None = None
    quota_total_seconds: int
    quota_remaining_seconds: int
    quota_used_seconds: int
    quota_package_count: int
    quota_active_package_count: int
    quota_exhausted_package_count: int
    quota_packages: list[AsrQuotaPackageOut]
    quota_fetch_error_message: str | None = None
    usage_ranges: list[AsrUsageRangeOut]
    usage_error_message: str | None = None


class AsrRequestEventOut(BaseModel):
    id: str
    source: Literal["local_audit", "cloud_audit"]
    action: str
    occurred_at: str | None = None
    status: Literal["submitted", "completed", "submit_failed", "task_failed", "unknown"]
    audio_name: str | None = None
    audio_path: str | None = None
    source_id: str | None = None
    source_ip: str | None = None
    chunk_index: int | None = None
    chunk_count: int | None = None
    submitted_duration_ms: int | None = None
    recognized_duration_ms: int | None = None
    file_size_bytes: int | None = None
    request_id: str | None = None
    task_id: int | None = None
    error_code: str | None = None
    error_message: str | None = None
