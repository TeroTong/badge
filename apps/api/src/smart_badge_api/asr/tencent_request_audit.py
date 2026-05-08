from __future__ import annotations

import asyncio
import csv
import json
import logging
import uuid
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, Literal

from smart_badge_api.core.config import get_settings

logger = logging.getLogger(__name__)

RequestEventSource = Literal["local_audit", "cloud_audit"]
RequestEventStatus = Literal["submitted", "completed", "submit_failed", "task_failed", "unknown"]

_write_lock = asyncio.Lock()


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _parse_iso_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_date_bounds(value: date, *, end: bool) -> datetime:
    bound_time = time.max if end else time.min
    return datetime.combine(value, bound_time, tzinfo=UTC)


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _normalize_status(value: object) -> RequestEventStatus:
    text = str(value or "").strip()
    if text in {"submitted", "completed", "submit_failed", "task_failed", "unknown"}:
        return text  # type: ignore[return-value]
    return "unknown"


def _load_json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed Tencent ASR request audit row from %s", path)
                    continue
                if isinstance(payload, dict):
                    items.append(payload)
    except OSError:
        logger.exception("Failed to read Tencent ASR request audit log: %s", path)
    return items


async def append_tencent_request_event(
    *,
    occurred_at: datetime,
    status: RequestEventStatus,
    audio_name: str | None,
    audio_path: str | None,
    source_id: str | None,
    chunk_index: int | None,
    chunk_count: int | None,
    submitted_duration_ms: int | None,
    recognized_duration_ms: int | None,
    file_size_bytes: int | None,
    request_id: str | None,
    task_id: int | None,
    error_code: str | None,
    error_message: str | None,
) -> None:
    path = get_settings().resolved_tencent_asr_request_audit_log_path
    payload = {
        "id": str(uuid.uuid4()),
        "source": "local_audit",
        "action": "CreateRecTask",
        "occurred_at": _serialize_datetime(occurred_at),
        "status": status,
        "audio_name": audio_name,
        "audio_path": audio_path,
        "source_id": source_id,
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "submitted_duration_ms": submitted_duration_ms,
        "recognized_duration_ms": recognized_duration_ms,
        "file_size_bytes": file_size_bytes,
        "request_id": request_id,
        "task_id": task_id,
        "error_code": error_code,
        "error_message": error_message,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        async with _write_lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("Failed to append Tencent ASR request audit event to %s", path)


def load_local_tencent_request_events() -> list[dict[str, Any]]:
    path = get_settings().resolved_tencent_asr_request_audit_log_path
    rows = _load_json_lines(path)
    items: list[dict[str, Any]] = []
    for row in rows:
        occurred_at = _parse_iso_datetime(row.get("occurred_at"))
        items.append(
            {
                "id": str(row.get("id") or uuid.uuid4()),
                "source": "local_audit",
                "action": str(row.get("action") or "CreateRecTask"),
                "occurred_at": _serialize_datetime(occurred_at),
                "status": _normalize_status(row.get("status")),
                "audio_name": str(row.get("audio_name") or "").strip() or None,
                "audio_path": str(row.get("audio_path") or "").strip() or None,
                "source_id": str(row.get("source_id") or "").strip() or None,
                "chunk_index": _coerce_int(row.get("chunk_index")),
                "chunk_count": _coerce_int(row.get("chunk_count")),
                "submitted_duration_ms": _coerce_int(row.get("submitted_duration_ms")),
                "recognized_duration_ms": _coerce_int(row.get("recognized_duration_ms")),
                "file_size_bytes": _coerce_int(row.get("file_size_bytes")),
                "request_id": str(row.get("request_id") or "").strip() or None,
                "task_id": _coerce_int(row.get("task_id")),
                "error_code": str(row.get("error_code") or "").strip() or None,
                "error_message": str(row.get("error_message") or "").strip() or None,
                "source_ip": None,
            }
        )
    return items


def load_cloud_audit_tencent_request_events() -> list[dict[str, Any]]:
    path = get_settings().resolved_tencent_asr_cloud_audit_log_path
    if not path.exists():
        return []

    items: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if str(row.get("EventName") or "").strip() != "CreateRecTask":
                    continue
                cloud_audit_raw = str(row.get("CloudAuditEvent") or "").strip()
                if not cloud_audit_raw:
                    continue
                try:
                    payload = json.loads(cloud_audit_raw)
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed CloudAudit row from %s", path)
                    continue

                event_time = _coerce_int(payload.get("eventTime"))
                occurred_at = (
                    datetime.fromtimestamp(event_time, UTC)
                    if event_time is not None
                    else _parse_iso_datetime(row.get("EventTime"))
                )
                api_error_message = str(payload.get("apiErrorMessage") or "").strip()
                request_id = str(payload.get("requestID") or row.get("RequestID") or "").strip() or None
                source_ip = str(payload.get("sourceIPAddress") or row.get("SourceIPAddress") or "").strip() or None
                status: RequestEventStatus = "submitted"
                error_code: str | None = None
                error_message: str | None = None
                if api_error_message:
                    status = "submit_failed"
                    error_message = api_error_message
                    error_code = api_error_message.split(":", 1)[0].strip() or None

                items.append(
                    {
                        "id": request_id or str(uuid.uuid4()),
                        "source": "cloud_audit",
                        "action": "CreateRecTask",
                        "occurred_at": _serialize_datetime(occurred_at),
                        "status": status,
                        "audio_name": None,
                        "audio_path": None,
                        "source_id": None,
                        "chunk_index": None,
                        "chunk_count": None,
                        "submitted_duration_ms": None,
                        "recognized_duration_ms": None,
                        "file_size_bytes": None,
                        "request_id": request_id,
                        "task_id": None,
                        "error_code": error_code,
                        "error_message": error_message,
                        "source_ip": source_ip,
                    }
                )
    except OSError:
        logger.exception("Failed to read Tencent CloudAudit log: %s", path)
    return items


def list_tencent_request_events(
    *,
    source: Literal["all", "local_audit", "cloud_audit"] = "all",
    status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if source in {"all", "local_audit"}:
        items.extend(load_local_tencent_request_events())
    if source in {"all", "cloud_audit"}:
        items.extend(load_cloud_audit_tencent_request_events())

    lower_bound = _parse_date_bounds(date_from, end=False) if date_from else None
    upper_bound = _parse_date_bounds(date_to, end=True) if date_to else None
    normalized_status = str(status or "").strip()

    filtered: list[dict[str, Any]] = []
    for item in items:
        occurred_at = _parse_iso_datetime(item.get("occurred_at"))
        if lower_bound and (occurred_at is None or occurred_at < lower_bound):
            continue
        if upper_bound and (occurred_at is None or occurred_at > upper_bound):
            continue
        if normalized_status and str(item.get("status") or "") != normalized_status:
            continue
        filtered.append(item)

    filtered.sort(
        key=lambda item: _parse_iso_datetime(item.get("occurred_at")) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return filtered


def summarize_tencent_request_events() -> dict[str, Any]:
    local_events = load_local_tencent_request_events()
    cloud_events = load_cloud_audit_tencent_request_events()

    def _event_key(item: dict[str, Any]) -> str:
        return (
            str(item.get("request_id") or "").strip()
            or str(item.get("task_id") or "").strip()
            or str(item.get("id") or "").strip()
        )

    local_by_request: dict[str, list[dict[str, Any]]] = {}
    for item in local_events:
        local_by_request.setdefault(_event_key(item), []).append(item)

    local_exact_count = len(local_by_request)
    local_success_count = 0
    local_failed_count = 0
    local_submitted_duration_ms = 0
    local_recognized_duration_ms = 0
    for events in local_by_request.values():
        statuses = {str(item.get("status") or "") for item in events}
        if "completed" in statuses:
            local_success_count += 1
        elif statuses & {"submit_failed", "task_failed"}:
            local_failed_count += 1

        submitted_event = next(
            (item for item in events if str(item.get("status") or "") == "submitted"),
            None,
        )
        failed_submit_event = next(
            (item for item in events if str(item.get("status") or "") == "submit_failed"),
            None,
        )
        duration_source = submitted_event or failed_submit_event
        if duration_source is not None:
            local_submitted_duration_ms += _coerce_int(duration_source.get("submitted_duration_ms")) or 0

        completed_event = next(
            (item for item in events if str(item.get("status") or "") == "completed"),
            None,
        )
        if completed_event is not None:
            local_recognized_duration_ms += _coerce_int(completed_event.get("recognized_duration_ms")) or 0

    cloud_total_count = len(cloud_events)
    cloud_failed_count = sum(1 for item in cloud_events if str(item.get("status") or "") == "submit_failed")

    latest_event = None
    combined = list_tencent_request_events(source="all")
    if combined:
        latest_event = combined[0]

    latest_error = None
    for item in combined:
        if item.get("error_code") or item.get("error_message"):
            latest_error = item
            break

    quota_state: Literal["normal", "exhausted", "unknown"] = "unknown"
    quota_message = None
    if latest_error is not None:
        error_code = str(latest_error.get("error_code") or "").strip()
        error_message = str(latest_error.get("error_message") or "").strip()
        if error_code == "FailedOperation.UserHasNoAmount" or "UserHasNoAmount" in error_message:
            quota_state = "exhausted"
            quota_message = "最近一次腾讯云 ASR 返回资源包额度不足。"
        else:
            quota_state = "normal"
            quota_message = error_message or None
    elif latest_event is not None and str(latest_event.get("status") or "") in {"submitted", "completed"}:
        quota_state = "normal"

    return {
        "local_exact_count": local_exact_count,
        "local_success_count": local_success_count,
        "local_failed_count": local_failed_count,
        "local_submitted_duration_ms": local_submitted_duration_ms,
        "local_recognized_duration_ms": local_recognized_duration_ms,
        "cloud_total_count": cloud_total_count,
        "cloud_failed_count": cloud_failed_count,
        "latest_event_at": latest_event.get("occurred_at") if latest_event else None,
        "latest_error_message": str(latest_error.get("error_message") or "").strip() or None if latest_error else None,
        "quota_state": quota_state,
        "quota_message": quota_message,
    }
