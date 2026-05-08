from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from smart_badge_api.asr.tencent_cloud_provider import (
    get_file_recognition_resource_packages,
    get_usage_totals_by_date_range,
)
from smart_badge_api.asr.tencent_request_audit import list_tencent_request_events, summarize_tencent_request_events
from smart_badge_api.core.config import get_settings
from smart_badge_api.schemas.asr_monitoring import (
    AsrMonitoringOverviewOut,
    AsrRequestEventOut,
    AsrUsageRangeOut,
)
from smart_badge_api.schemas.pagination import PaginatedResponse, make_page_response

router = APIRouter(prefix="/asr-monitoring", tags=["ASR监控"])

_CHINA_TZ = ZoneInfo("Asia/Shanghai")


def _to_request_event_out(item: dict) -> AsrRequestEventOut:
    return AsrRequestEventOut(
        id=str(item.get("id") or ""),
        source=item.get("source") or "local_audit",
        action=str(item.get("action") or "CreateRecTask"),
        occurred_at=item.get("occurred_at"),
        status=item.get("status") or "unknown",
        audio_name=item.get("audio_name"),
        audio_path=item.get("audio_path"),
        source_id=item.get("source_id"),
        source_ip=item.get("source_ip"),
        chunk_index=item.get("chunk_index"),
        chunk_count=item.get("chunk_count"),
        submitted_duration_ms=item.get("submitted_duration_ms"),
        recognized_duration_ms=item.get("recognized_duration_ms"),
        file_size_bytes=item.get("file_size_bytes"),
        request_id=item.get("request_id"),
        task_id=item.get("task_id"),
        error_code=item.get("error_code"),
        error_message=item.get("error_message"),
    )


@router.get("/overview", response_model=AsrMonitoringOverviewOut)
async def get_asr_monitoring_overview():
    settings = get_settings()
    summary = summarize_tencent_request_events()
    request_log_path = settings.resolved_tencent_asr_request_audit_log_path
    cloud_audit_log_path = settings.resolved_tencent_asr_cloud_audit_log_path

    usage_ranges: list[AsrUsageRangeOut] = []
    usage_error_message: str | None = None
    quota_total_seconds = 0
    quota_remaining_seconds = 0
    quota_used_seconds = 0
    quota_package_count = 0
    quota_active_package_count = 0
    quota_exhausted_package_count = 0
    quota_packages: list[dict] = []
    quota_fetch_error_message: str | None = None
    has_tencent_credentials = bool(
        settings.tencent_asr_secret_id.strip() and settings.tencent_asr_secret_key.strip()
    )

    if has_tencent_credentials:
        today = datetime.now(_CHINA_TZ).date()
        ranges = [
            ("今日官方用量", today, today),
            ("近 7 天官方用量", today - timedelta(days=6), today),
            ("近 30 天官方用量", today - timedelta(days=29), today),
        ]
        # 4 个独立的腾讯云 API 调用并发执行，避免串行等待。
        usage_results, quota_result = await asyncio.gather(
            asyncio.gather(
                *[
                    get_usage_totals_by_date_range(
                        start_date=start_date,
                        end_date=end_date,
                        biz_name_list=["asr_rec"],
                    )
                    for _, start_date, end_date in ranges
                ],
                return_exceptions=True,
            ),
            asyncio.gather(get_file_recognition_resource_packages(), return_exceptions=True),
        )

        for (label, start_date, end_date), usage in zip(ranges, usage_results):
            if isinstance(usage, BaseException):
                if usage_error_message is None:
                    usage_error_message = str(usage)
                continue
            totals = usage.get("asr_rec") or {"count": 0, "duration": 0}
            usage_ranges.append(
                AsrUsageRangeOut(
                    label=label,
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat(),
                    request_count=int(totals.get("count") or 0),
                    duration_seconds=int(totals.get("duration") or 0),
                )
            )

        quota_summary_or_exc = quota_result[0]
        if isinstance(quota_summary_or_exc, BaseException):
            quota_fetch_error_message = str(quota_summary_or_exc)
        else:
            quota_summary = quota_summary_or_exc
            quota_total_seconds = int(quota_summary.get("total_seconds") or 0)
            quota_remaining_seconds = int(quota_summary.get("remaining_seconds") or 0)
            quota_used_seconds = int(quota_summary.get("used_seconds") or 0)
            quota_package_count = int(quota_summary.get("package_count") or 0)
            quota_active_package_count = int(quota_summary.get("active_package_count") or 0)
            quota_exhausted_package_count = int(quota_summary.get("exhausted_package_count") or 0)
            quota_packages = list(quota_summary.get("packages") or [])

    return AsrMonitoringOverviewOut(
        provider=settings.asr_provider,
        has_tencent_credentials=has_tencent_credentials,
        request_log_available=request_log_path.exists(),
        cloud_audit_log_available=cloud_audit_log_path.exists(),
        quota_state=summary["quota_state"],
        quota_message=summary["quota_message"],
        local_exact_count=summary["local_exact_count"],
        local_success_count=summary["local_success_count"],
        local_failed_count=summary["local_failed_count"],
        local_submitted_duration_ms=summary["local_submitted_duration_ms"],
        local_recognized_duration_ms=summary["local_recognized_duration_ms"],
        cloud_total_count=summary["cloud_total_count"],
        cloud_failed_count=summary["cloud_failed_count"],
        latest_event_at=summary["latest_event_at"],
        latest_error_message=summary["latest_error_message"],
        quota_total_seconds=quota_total_seconds,
        quota_remaining_seconds=quota_remaining_seconds,
        quota_used_seconds=quota_used_seconds,
        quota_package_count=quota_package_count,
        quota_active_package_count=quota_active_package_count,
        quota_exhausted_package_count=quota_exhausted_package_count,
        quota_packages=quota_packages,
        quota_fetch_error_message=quota_fetch_error_message,
        usage_ranges=usage_ranges,
        usage_error_message=usage_error_message,
    )


@router.get("/requests", response_model=PaginatedResponse[AsrRequestEventOut])
async def list_asr_monitoring_requests(
    source: str = Query(default="all", pattern="^(all|local_audit|cloud_audit)$"),
    status: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    rows = list_tencent_request_events(
        source=source,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )
    total = len(rows)
    start = (page - 1) * page_size
    end = start + page_size
    sliced = rows[start:end]
    return make_page_response([_to_request_event_out(item) for item in sliced], total, page, page_size)
