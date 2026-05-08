from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.archive_access import archive_item_visible_to_scope
from smart_badge_api.api.routes.dingtalk import (
    _attach_archive_recording_bindings,
    _clean_text as _archive_clean_text,
    _coerce_datetime as _archive_coerce_datetime,
    _load_archive_recording_index,
)
from smart_badge_api.core.permissions import PermissionScope
from smart_badge_api.db.models import Recording, Visit
from smart_badge_api.schemas.recordings import PendingArchiveRecordingOut
from smart_badge_api.visit_linking import ordered_visit_recording_links

_ARCHIVE_EXCLUDED_PIPELINE_STATUSES = {"filtered", "failed"}
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _parse_visit_time(value: str | None) -> time | None:
    if not value:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt).time()
        except ValueError:
            continue
    return None


def resolve_visit_display_datetime(visit: Visit) -> datetime | None:
    if visit.visit_date:
        parsed_time = _parse_visit_time(visit.visit_time)
        if parsed_time is not None:
            return datetime.combine(visit.visit_date, parsed_time)
    if visit.created_at is not None:
        return visit.created_at
    if visit.visit_date:
        return datetime.combine(visit.visit_date, time.min)
    return None


def _archive_item_recorded_at(item: dict[str, Any]) -> datetime | None:
    for key in ("create_time", "downloaded_at", "updated_at"):
        resolved = _archive_coerce_datetime(item.get(key))
        if resolved is not None:
            return resolved
    return None


def _as_comparable_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(_BUSINESS_TZ).replace(tzinfo=None)


def _pending_archive_recorded_timestamp(item: PendingArchiveRecordingOut) -> float:
    recorded_at = _archive_coerce_datetime(item.create_time)
    return recorded_at.timestamp() if recorded_at is not None else 0.0


def _visit_recording_ids(visit: Visit) -> set[str]:
    merged: dict[str, Recording] = {}

    for link in ordered_visit_recording_links(visit):
        recording = link.recording
        if recording is None or recording.id in merged:
            continue
        merged[recording.id] = recording

    for recording in visit.recordings or []:
        if recording.id in merged:
            continue
        merged[recording.id] = recording

    return set(merged.keys())


def _build_match_payload(
    item: dict[str, Any],
    *,
    score: float,
    reasons: list[str],
) -> PendingArchiveRecordingOut:
    return PendingArchiveRecordingOut(
        id=str(item.get("id") or ""),
        display_file_name=_archive_clean_text(item.get("display_file_name")) or "未命名录音",
        create_time=_archive_clean_text(item.get("create_time")),
        duration_seconds=item.get("duration_seconds"),
        staff_id=_archive_clean_text(item.get("staff_id")),
        staff_name=_archive_clean_text(item.get("staff_name")),
        device_code=_archive_clean_text(item.get("device_code")) or _archive_clean_text(item.get("sn")),
        pipeline_status=_archive_clean_text(item.get("pipeline_status")),
        recording_id=_archive_clean_text(item.get("recording_id")),
        has_transcript=bool(item.get("has_transcript")),
        has_analysis=bool(item.get("has_analysis")),
        match_score=round(score, 2),
        match_reasons=reasons,
    )


def _match_archive_item_to_visit(item: dict[str, Any], visit: Visit) -> PendingArchiveRecordingOut | None:
    if not bool(item.get("has_transcript")):
        return None
    if str(item.get("pipeline_status") or "").strip().lower() in _ARCHIVE_EXCLUDED_PIPELINE_STATUSES:
        return None
    if not bool(item.get("needs_visit_link")):
        return None

    linked_visit_ids = {str(value) for value in (item.get("linked_visit_ids") or [])}
    if visit.id in linked_visit_ids:
        return None

    candidate_recorded_at = _as_comparable_datetime(_archive_item_recorded_at(item))
    visit_display_at = _as_comparable_datetime(resolve_visit_display_datetime(visit))
    if candidate_recorded_at is None or visit_display_at is None:
        return None

    day_delta = abs((candidate_recorded_at.date() - visit_display_at.date()).days)
    if day_delta != 0:
        return None

    visit_recording_ids = _visit_recording_ids(visit)
    recording_id = _archive_clean_text(item.get("recording_id"))
    if recording_id and recording_id in visit_recording_ids:
        return None

    consultant_id = _archive_clean_text(visit.consultant_id)
    consultant_name = _archive_clean_text(visit.consultant.name if visit.consultant else None)
    consultant_badge_id = _archive_clean_text(visit.consultant.badge_id if visit.consultant else None)
    item_staff_id = _archive_clean_text(item.get("staff_id"))
    item_staff_name = _archive_clean_text(item.get("staff_name"))
    item_device_code = _archive_clean_text(item.get("device_code")) or _archive_clean_text(item.get("sn"))

    score = 2.0
    reasons = ["录音日期与本次到诊一致"]

    consultant_matched = False
    if consultant_id and item_staff_id == consultant_id:
        consultant_matched = True
        score += 3.6
        reasons.append("录音上传者与本次咨询师一致")
    elif consultant_name and item_staff_name == consultant_name:
        consultant_matched = True
        score += 3.0
        reasons.append("录音上传者姓名与本次咨询师一致")
    elif consultant_badge_id and item_device_code == consultant_badge_id:
        consultant_matched = True
        score += 2.8
        reasons.append("录音工牌号与本次咨询师一致")

    has_consultant_identity = bool(consultant_id or consultant_name or consultant_badge_id)
    if has_consultant_identity and not consultant_matched:
        return None

    minute_delta = abs((candidate_recorded_at - visit_display_at).total_seconds()) / 60
    if minute_delta <= 30:
        score += 3.2
        reasons.append(f"录音时间在到诊时间前后 {round(minute_delta)} 分钟内")
    elif minute_delta <= 90:
        score += 2.4
        reasons.append(f"录音时间与到诊时间较接近（相差 {round(minute_delta)} 分钟）")
    elif minute_delta <= 240:
        score += 1.2
        reasons.append(f"录音时间与到诊时间同日但相差较大（{round(minute_delta)} 分钟）")
    elif not consultant_matched:
        return None

    if not consultant_matched and minute_delta > 120:
        return None

    if bool(item.get("has_analysis")):
        score += 0.4
        reasons.append("该录音已完成分析")

    return _build_match_payload(item, score=score, reasons=reasons[:3])


async def build_pending_archive_recordings_by_visit_id(
    db: AsyncSession,
    visits: list[Visit],
    scope: PermissionScope,
    *,
    limit_per_visit: int = 3,
) -> dict[str, list[PendingArchiveRecordingOut]]:
    if not visits:
        return {}

    archive_index = _load_archive_recording_index()
    if not archive_index:
        return {}

    summaries = [dict(payload["summary"]) for payload in archive_index.values()]
    summaries = await _attach_archive_recording_bindings(db, summaries)
    visible_items = [item for item in summaries if archive_item_visible_to_scope(item, scope)]

    pending_by_visit_id: dict[str, list[PendingArchiveRecordingOut]] = {}
    for visit in visits:
        if _visit_recording_ids(visit):
            continue
        candidates = [
            candidate
            for candidate in (
                _match_archive_item_to_visit(item, visit)
                for item in visible_items
            )
            if candidate is not None
        ]
        candidates.sort(
            key=lambda item: (
                item.match_score,
                _pending_archive_recorded_timestamp(item),
            ),
            reverse=True,
        )
        if candidates:
            pending_by_visit_id[visit.id] = candidates[:limit_per_visit]

    return pending_by_visit_id
