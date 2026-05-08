from __future__ import annotations

import logging
import mimetypes
import re
import asyncio
import time
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import aiofiles
from fastapi import APIRouter, Body, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from smart_badge_api.analysis.customer_profile_score_sync import refresh_customer_profile_scores
from smart_badge_api.api.archive_access import (
    archive_item_hospital_code,
)
from smart_badge_api.api.analysis_normalization import normalize_task_detail
from smart_badge_api.api.data_scope import (
    build_permission_scope,
    recording_scope_condition,
    visit_order_scope_condition,
    visit_scope_condition,
)
from smart_badge_api.api.deps import get_current_user, require_system_admin_or_above
from smart_badge_api.core.config import get_settings
from smart_badge_api.core.permissions import normalize_permission_role, permission_role_level
from smart_badge_api.asr.tencent_media_proxy import build_tencent_media_path
from smart_badge_api.db.models import (
    AnalysisTask,
    Customer,
    Device,
    Recording,
    RecordingVisitAnalysis,
    RecordingVisitLink,
    Staff,
    StaffManagementRelation,
    Transcript,
    User,
    Visit,
    VisitOrder,
    _new_id,
)
from smart_badge_api.db.session import get_db
from smart_badge_api.recording_multi_customer import (
    confirm_multi_customer_mappings,
    ensure_multi_customer_review,
    reset_multi_customer_mappings,
    sync_visit_analysis_results,
)
from smart_badge_api.recording_analysis_service import create_or_dispatch_recording_analysis
from smart_badge_api.schemas.matching import RecordingVisitOrderMatchOut
from smart_badge_api.schemas.pagination import PaginatedResponse, make_page_response
from smart_badge_api.schemas.recordings import (
    ArchiveRecordingDateSummaryOut,
    ArchiveRecordingDetailOut,
    ArchiveRecordingEnsureOut,
    ArchiveRecordingOut,
    ArchiveRecordingPageOut,
    RecordingMediaSourceOut,
    RecordingMultiCustomerConfirmRequest,
    RecordingMultiCustomerReviewOut,
    RecordingOut,
    RecordingSplitOut,
    RecordingSplitPartOut,
    RecordingSplitRequest,
    RecordingUpdate,
    SapPushDispatchOut,
    SapPushDispatchRequest,
    SapPushLogOut,
    SapPushPreviewOut,
)
from smart_badge_api.recording_split_service import (
    RecordingSplitError,
    SplitTranscriptPart,
    split_audio_file,
    split_transcript_utterances,
    write_split_archive_manifest,
)
from smart_badge_api.schemas.tasks import TaskDetailOut, TaskOut
from smart_badge_api.api.routes.dingtalk import (
    _attach_archive_recording_bindings,
    _build_archive_analysis_summary,
    _clean_text as _archive_clean_text,
    _coerce_datetime as _archive_coerce_datetime,
    _ensure_archive_recording_entry,
    _load_archive_recording_index,
    _resolve_archive_analysis_result,
    _resolve_archive_recording_audio_path,
    _resolve_archive_transcript,
)
from smart_badge_api.sap_push_service import (
    SapPushPreparationError,
    create_sap_push_log,
    execute_sap_push_log,
    list_recording_sap_push_logs,
    serialize_sap_push_log,
)
from smart_badge_api.task_queue import dispatch_sap_push_log
from smart_badge_api.sap_consultation import generate_sap_consultation_payloads
from smart_badge_api.visit_linking import ordered_recording_visit_links, sync_recording_visit_links
from smart_badge_api.visit_order_matching import (
    _department_assistant_order_match,
    _load_staff_position_text,
    analyze_recording_visit_order_match,
)
from smart_badge_api.visit_order_sync import (
    _build_visit_notes,
    _compute_customer_current_age,
    _first_non_empty,
    _format_time,
    _parse_jdrq,
    _visit_created_at_from_order,
    _visit_status_from_order,
    retry_visit_order_sync,
    sync_visit_orders_for_recording,
)

router = APIRouter(prefix="/recordings", tags=["recordings"])
_ARCHIVE_RECORDING_LIST_CACHE_TTL_SECONDS = 60.0
_archive_recording_list_cache: dict[str, object] = {
    "expires_at": 0.0,
    "source_key": None,
    "items": None,
}
_archive_recording_list_lock = asyncio.Lock()
logger = logging.getLogger("smart_badge.recordings")

ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm", ".amr"}


class RecordingVisitOrderLocalVisitRequest(BaseModel):
    visit_order_id: str


class RecordingVisitOrderLocalVisitOut(BaseModel):
    visit_id: str
    visit_order_id: str
    dzdh: str | None = None
    dzseg: str | None = None


async def _sync_visit_orders_for_recording_context(db: AsyncSession, recording: Recording) -> None:
    try:
        result = await retry_visit_order_sync(
            lambda: sync_visit_orders_for_recording(db, recording),
            label=f"recording-context:{recording.id}",
            attempts=3,
            initial_delay_seconds=1.0,
        )
        if result.new_count or result.updated_count:
            logger.info(
                "synced visit orders for recording context recording_id=%s new=%d updated=%d",
                recording.id,
                result.new_count,
                result.updated_count,
            )
    except Exception:
        logger.exception("failed to sync visit orders for recording context recording_id=%s", recording.id)


def _to_out(recording: Recording, device_code: str | None = None) -> RecordingOut:
    linked_visits = [
        RecordingOut.LinkedVisitOut(
            id=link.visit.id,
            external_visit_order_no=link.visit.external_visit_order_no,
            external_visit_order_seg=link.visit.external_visit_order_seg,
            customer_name=link.visit.customer.name if link.visit.customer else None,
            is_primary=link.is_primary,
        )
        for link in ordered_recording_visit_links(recording)
        if link.visit is not None
    ]
    return RecordingOut(
        id=recording.id,
        visit_id=recording.visit_id,
        linked_visit_ids=[item.id for item in linked_visits],
        linked_visits=linked_visits,
        visit_status=recording.visit.status if recording.visit else None,
        staff_id=recording.staff_id,
        staff_name=recording.staff.name if recording.staff else None,
        staff_badge_id=recording.staff.badge_id if recording.staff else None,
        staff_role=recording.staff.role if recording.staff else None,
        customer_name=recording.visit.customer.name if recording.visit and recording.visit.customer else None,
        device_id=recording.device_id,
        device_code=device_code,
        file_name=recording.file_name,
        file_size=recording.file_size,
        duration_seconds=recording.duration_seconds,
        status=recording.status,
        split_parent_recording_id=recording.split_parent_recording_id,
        split_part_index=recording.split_part_index,
        split_at_ms=recording.split_at_ms,
        has_transcript=recording.transcript is not None,
        created_at=recording.created_at.isoformat() if recording.created_at else "",
    )


def _serialize_multi_customer_review(recording: Recording) -> RecordingMultiCustomerReviewOut:
    links = [link for link in ordered_recording_visit_links(recording) if link.visit is not None]
    analyses_by_visit_id = {analysis.visit_id: analysis for analysis in recording.visit_analyses}
    mapped_segment_ids = {
        analysis.customer_segment_id: analysis.visit_id
        for analysis in recording.visit_analyses
        if analysis.customer_segment_id
    }
    segments = [
        {
            "id": segment.id,
            "segment_index": segment.segment_index,
            "label": segment.label,
            "begin_ms": segment.begin_ms,
            "end_ms": segment.end_ms,
            "summary": segment.summary,
            "utterance_count": segment.utterance_count,
            "status": segment.status,
            "mapped_visit_id": mapped_segment_ids.get(segment.id),
        }
        for segment in sorted(recording.customer_segments, key=lambda item: item.segment_index)
    ]
    visit_analyses = []
    for link in links:
        visit = link.visit
        analysis = analyses_by_visit_id.get(link.visit_id)
        visit_analyses.append(
            {
                "id": analysis.id if analysis else "",
                "recording_id": recording.id,
                "visit_id": link.visit_id,
                "visit_order_no": visit.external_visit_order_no if visit else None,
                "visit_order_seg": visit.external_visit_order_seg if visit else None,
                "customer_name": visit.customer.name if visit and visit.customer else None,
                "customer_code": visit.customer.external_customer_code if visit and visit.customer else None,
                "customer_segment_id": analysis.customer_segment_id if analysis else None,
                "mapping_status": analysis.mapping_status if analysis else "pending",
                "analysis_status": analysis.analysis_status if analysis else "idle",
                "analysis_task_id": analysis.analysis_task_id if analysis else None,
                "analysis_error": analysis.analysis_error if analysis else None,
                "confirmed_by": analysis.confirmed_by if analysis else None,
                "confirmed_at": analysis.confirmed_at.isoformat() if analysis and analysis.confirmed_at else None,
                "sap_ready_at": analysis.sap_ready_at.isoformat() if analysis and analysis.sap_ready_at else None,
                "sap_push_log_id": analysis.sap_push_log_id if analysis else None,
            }
        )

    required = len(links) > 1
    if not required:
        status = "not_required"
        message = "当前录音只关联一张到诊单，不需要多客户对应确认。"
    elif any(item["mapping_status"] != "confirmed" for item in visit_analyses):
        status = "pending_mapping"
        message = "当前录音关联了多张到诊单，需要先确认客户段与到诊单的对应关系。"
    elif any(item["analysis_status"] in {"pending", "running"} for item in visit_analyses):
        status = "analyzing"
        message = "客户对应关系已确认，正在分别生成到诊单级分析结果。"
    elif any(item["analysis_status"] == "failed" for item in visit_analyses):
        status = "failed"
        message = "部分到诊单级分析失败，请重新确认或重试分析。"
    elif all(item["analysis_status"] == "done" for item in visit_analyses):
        status = "ready"
        message = "多客户分析已完成，满足稳定等待时间后会按到诊单分别自动回传 SAP。"
    else:
        status = "pending_mapping"
        message = "当前录音需要完成多客户对应确认。"

    return RecordingMultiCustomerReviewOut.model_validate(
        {
            "recording_id": recording.id,
            "required": required,
            "linked_visit_count": len(links),
            "status": status,
            "message": message,
            "segments": segments,
            "visit_analyses": visit_analyses,
        }
    )


def _load_opts():
    return [
        selectinload(Recording.staff),
        selectinload(Recording.transcript),
        selectinload(Recording.visit).selectinload(Visit.customer),
        selectinload(Recording.visit_links).selectinload(RecordingVisitLink.visit).selectinload(Visit.customer),
        selectinload(Recording.customer_segments),
        selectinload(Recording.visit_analyses).selectinload(RecordingVisitAnalysis.customer_segment),
        selectinload(Recording.visit_analyses).selectinload(RecordingVisitAnalysis.analysis_task),
        selectinload(Recording.visit_analyses).selectinload(RecordingVisitAnalysis.visit).selectinload(Visit.customer),
    ]


def _ensure_upload_dir() -> Path:
    recordings_dir = get_settings().upload_path / "recordings"
    recordings_dir.mkdir(parents=True, exist_ok=True)
    return recordings_dir


async def _refresh_customer_profile_scores_for_recording_links(
    db: AsyncSession,
    recording_id: str,
) -> None:
    recording = (
        await db.execute(
            select(Recording)
            .where(Recording.id == recording_id)
            .options(selectinload(Recording.visit_links))
        )
    ).scalar_one_or_none()
    if recording is None:
        return

    visit_ids = {
        str(visit_id)
        for visit_id in [
            recording.visit_id,
            *[link.visit_id for link in (recording.visit_links or [])],
        ]
        if str(visit_id or "").strip()
    }
    if not visit_ids:
        return

    customer_ids = (
        await db.execute(select(Visit.customer_id).where(Visit.id.in_(visit_ids)))
    ).scalars().all()
    refreshed_customer_ids: set[str] = set()
    for customer_id in customer_ids:
        normalized_customer_id = str(customer_id or "").strip()
        if not normalized_customer_id or normalized_customer_id in refreshed_customer_ids:
            continue
        refreshed_customer_ids.add(normalized_customer_id)
        await refresh_customer_profile_scores(db, normalized_customer_id)


async def _get_scoped_recording(recording_id: str, db: AsyncSession, current_user: User) -> Recording | None:
    scope = await build_permission_scope(current_user)
    return (
        await db.execute(select(Recording).where(Recording.id == recording_id, recording_scope_condition(scope)).options(*_load_opts()))
    ).scalar_one_or_none()


def _recording_date_candidates(recording: Recording) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append(normalized)

    if recording.created_at:
        add(recording.created_at.strftime("%Y-%m-%d") if hasattr(recording.created_at, "strftime") else str(recording.created_at)[:10])

    file_name = str(recording.file_name or "").strip()
    match_full = re.search(r"(\d{4})(\d{2})(\d{2})", file_name)
    if match_full:
        add(f"{match_full.group(1)}-{match_full.group(2)}-{match_full.group(3)}")
    else:
        match_mmdd = re.match(r"^(\d{2})(\d{2})_\d{6}(?:\.[A-Za-z0-9]+)?$", file_name)
        if match_mmdd and recording.created_at:
            add(f"{recording.created_at.year:04d}-{match_mmdd.group(1)}-{match_mmdd.group(2)}")

    return candidates


async def _visit_ids_linkable_by_recording_context(
    db: AsyncSession,
    *,
    scope,
    recording: Recording,
    visit_ids: list[str],
) -> set[str]:
    date_candidates = _recording_date_candidates(recording)
    hospital_codes = {str(scope.hospital_code or "").strip()}
    if recording.staff_id:
        recording_staff_hospital_code = (
            await db.execute(select(Staff.hospital_code).where(Staff.id == recording.staff_id))
        ).scalar_one_or_none()
        if recording_staff_hospital_code:
            hospital_codes.add(str(recording_staff_hospital_code).strip())
    hospital_codes.discard("")
    if not date_candidates or not hospital_codes:
        return set()

    return set(
        (
            await db.execute(
                select(Visit.id)
                .join(VisitOrder, VisitOrder.dzdh == Visit.external_visit_order_no)
                .where(
                    Visit.id.in_(visit_ids),
                    Visit.external_visit_order_no.is_not(None),
                    VisitOrder.jgbm.in_(hospital_codes),
                    or_(VisitOrder.crtdt.in_(date_candidates), VisitOrder.sjrq.in_(date_candidates)),
                )
            )
        ).scalars().all()
    )


async def _ensure_visit_ids_in_scope(
    db: AsyncSession,
    current_user: User,
    visit_ids: list[str | None],
    *,
    recording: Recording | None = None,
) -> None:
    scope = await build_permission_scope(current_user)
    normalized_visit_ids = list(dict.fromkeys(str(visit_id or "").strip() for visit_id in visit_ids if str(visit_id or "").strip()))
    if not normalized_visit_ids:
        return
    accessible_visit_ids = set(
        (
            await db.execute(
                select(Visit.id).where(
                    Visit.id.in_(normalized_visit_ids),
                    visit_scope_condition(scope),
                )
            )
        ).scalars().all()
    )
    denied_visit_ids = [visit_id for visit_id in normalized_visit_ids if visit_id not in accessible_visit_ids]
    if denied_visit_ids and recording is not None:
        context_linkable_ids = await _visit_ids_linkable_by_recording_context(
            db,
            scope=scope,
            recording=recording,
            visit_ids=denied_visit_ids,
        )
        denied_visit_ids = [visit_id for visit_id in denied_visit_ids if visit_id not in context_linkable_ids]
    if denied_visit_ids:
        raise HTTPException(403, "存在无权限关联的到诊单，请刷新候选列表后重试")


def _parse_visit_order_date(value: str | None) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


async def _get_linkable_visit_order_for_recording(
    db: AsyncSession,
    *,
    current_user: User,
    recording: Recording,
    visit_order_id: str,
) -> VisitOrder:
    normalized_visit_order_id = str(visit_order_id or "").strip()
    if not normalized_visit_order_id:
        raise HTTPException(400, "请选择到诊单")

    visit_order = await db.get(VisitOrder, normalized_visit_order_id)
    if visit_order is None:
        raise HTTPException(404, "到诊单不存在")

    scope = await build_permission_scope(current_user)
    in_user_scope = (
        await db.execute(
            select(VisitOrder.id).where(
                VisitOrder.id == visit_order.id,
                visit_order_scope_condition(scope),
            )
        )
    ).scalar_one_or_none()
    if in_user_scope:
        return visit_order

    date_candidates = set(_recording_date_candidates(recording))
    hospital_codes = {str(scope.hospital_code or "").strip()}
    recording_staff = await db.get(Staff, recording.staff_id) if recording.staff_id else None
    if recording_staff and recording_staff.hospital_code:
        hospital_codes.add(str(recording_staff.hospital_code).strip())
    hospital_codes.discard("")

    order_dates = {
        str(value or "").strip()
        for value in (visit_order.crtdt, visit_order.sjrq)
        if str(value or "").strip()
    }
    same_context = (
        bool(date_candidates.intersection(order_dates))
        and bool(hospital_codes)
        and str(visit_order.jgbm or "").strip() in hospital_codes
    )
    if same_context:
        return visit_order

    if recording_staff:
        staff_position_text = await _load_staff_position_text(db, recording_staff)
        if _department_assistant_order_match(recording_staff, staff_position_text, visit_order):
            return visit_order

    raise HTTPException(403, "无权限关联该到诊单，请刷新候选列表后重试")


async def _ensure_local_visit_for_visit_order(db: AsyncSession, visit_order: VisitOrder) -> Visit:
    dzdh = str(visit_order.dzdh or "").strip()
    if not dzdh:
        raise HTTPException(400, "到诊单缺少单号，无法创建本地接诊")

    group_orders = (
        await db.execute(
            select(VisitOrder)
            .where(
                VisitOrder.dzdh == dzdh,
                VisitOrder.jgbm == visit_order.jgbm,
            )
            .order_by(VisitOrder.dzseg.asc(), VisitOrder.fzdh.asc(), VisitOrder.id.asc())
        )
    ).scalars().all()
    if not group_orders:
        group_orders = [visit_order]
    primary_order = group_orders[0]

    visit = (
        await db.execute(
            select(Visit)
            .where(
                Visit.external_visit_order_no == dzdh,
                Visit.external_visit_order_seg == primary_order.dzseg,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if visit is None:
        visit = (
            await db.execute(
                select(Visit)
                .where(Visit.external_visit_order_no == dzdh)
                .order_by(Visit.created_at.asc(), Visit.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

    customer_code = str(primary_order.kunr or "").strip() or None
    customer_name = str(primary_order.ninam or "").strip() or f"客户 {dzdh}"
    customer = None
    if visit is not None:
        customer = await db.get(Customer, visit.customer_id)
    if customer is None and customer_code:
        customer = (
            await db.execute(
                select(Customer)
                .where(Customer.external_customer_code == customer_code)
                .limit(1)
            )
        ).scalar_one_or_none()
    if customer is None:
        customer = Customer(
            name=customer_name,
            external_customer_code=customer_code,
            gender=primary_order.customer_gender,
            age=_compute_customer_current_age(primary_order),
            source=primary_order.qdly1_txt or primary_order.dzly_txt,
            notes=None,
            created_at=_parse_jdrq(primary_order) or _visit_created_at_from_order(primary_order),
        )
        db.add(customer)
        await db.flush()
    else:
        customer.name = customer_name
        customer.source = primary_order.qdly1_txt or primary_order.dzly_txt or customer.source
        if primary_order.customer_gender and not customer.gender:
            customer.gender = primary_order.customer_gender
        computed_age = _compute_customer_current_age(primary_order)
        if computed_age is not None:
            customer.age = computed_age

    staff_codes = {
        str(value or "").strip()
        for value in (primary_order.fzr_id_dq, primary_order.fzuer)
        if str(value or "").strip()
    }
    staff_by_external_code: dict[str, Staff] = {}
    if staff_codes:
        staff_rows = (
            await db.execute(select(Staff).where(Staff.external_account.in_(staff_codes)))
        ).scalars().all()
        staff_by_external_code = {staff.external_account: staff for staff in staff_rows if staff.external_account}
    consultant = (
        staff_by_external_code.get(str(primary_order.fzr_id_dq or "").strip())
        or staff_by_external_code.get(str(primary_order.fzuer or "").strip())
    )

    if visit is None:
        visit = Visit(
            customer_id=customer.id,
            external_visit_order_no=dzdh,
            external_visit_order_seg=primary_order.dzseg,
            created_at=_visit_created_at_from_order(primary_order),
        )
        db.add(visit)
        await db.flush()

    visit.customer_id = customer.id
    visit.external_visit_order_no = dzdh
    visit.external_visit_order_seg = primary_order.dzseg
    visit.consultant_id = consultant.id if consultant else visit.consultant_id
    visit.visit_date = _parse_visit_order_date(primary_order.sjrq) or _parse_visit_order_date(primary_order.crtdt)
    visit.visit_time = _format_time(primary_order.fzsj)
    visit.deal_status = primary_order.jcsta_txt
    visit.arrival_purpose = primary_order.dymd_txt
    visit.project_needs = _first_non_empty(primary_order.remark_dz)
    visit.updated_at = _visit_created_at_from_order(primary_order)

    statuses = {_visit_status_from_order(order) for order in group_orders}
    status_priority = ("closed_won", "closed_lost", "diagnosed", "consulted", "assigned", "created")
    visit.status = next((status for status in status_priority if status in statuses), visit.status)

    if len(group_orders) == 1:
        visit.notes = _build_visit_notes(primary_order)
    else:
        notes_parts: list[str] = []
        for item in group_orders:
            seg_note = _build_visit_notes(item)
            advxc_label = item.advxc_long or item.advxc or ""
            seg_header = f"[行项目 {item.dzseg}"
            if advxc_label:
                seg_header += f" | {advxc_label}"
            seg_header += "]"
            notes_parts.append(f"{seg_header} {seg_note}" if seg_note else seg_header)
        visit.notes = "\n".join(notes_parts)

    await db.flush()
    return visit


def _can_split_recording(recording: Recording, user: User) -> bool:
    if permission_role_level(user.role) >= permission_role_level("hospital_admin"):
        return True
    user_staff_id = str(user.staff_id or "").strip()
    return bool(user_staff_id and recording.staff_id == user_staff_id)


def _recording_duration_ms(recording: Recording) -> int | None:
    if recording.duration_seconds and recording.duration_seconds > 0:
        return int(recording.duration_seconds * 1000)
    if recording.transcript and recording.transcript.duration_ms and recording.transcript.duration_ms > 0:
        return int(recording.transcript.duration_ms)
    utterances = recording.transcript.utterances if recording.transcript else recording.transcript_segments
    if isinstance(utterances, list):
        end_values = [
            int(item.get("end_ms") or 0)
            for item in utterances
            if isinstance(item, dict) and isinstance(item.get("end_ms"), int | float)
        ]
        if end_values:
            return max(end_values)
    return None


TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")
_RECORDING_NAME_START_PATTERN = re.compile(r"(?P<date>\d{4})_(?P<time>\d{6})(?:_(?P<segment_time>\d{6}))?")


def _recording_name_candidates(recording: Recording) -> list[str]:
    candidates: list[str] = []
    for value in (recording.file_name, recording.file_path):
        text = str(value or "").strip()
        if not text:
            continue
        leaf = Path(text).name
        if leaf and leaf not in candidates:
            candidates.append(leaf)
    return candidates


def _recording_local_datetime(recording: Recording) -> datetime:
    fallback = (recording.created_at or datetime.now(timezone.utc)).replace(microsecond=0)
    if fallback.tzinfo is None:
        fallback = fallback.replace(tzinfo=timezone.utc)
    return fallback.astimezone(TZ_SHANGHAI)


def _datetime_from_recording_name_match(match: re.Match[str], fallback: datetime) -> datetime | None:
    raw_date = match.group("date")
    origin_time = match.group("time")
    segment_time = match.group("segment_time")
    effective_time = segment_time or origin_time
    try:
        origin_at = fallback.replace(
            month=int(raw_date[:2]),
            day=int(raw_date[2:]),
            hour=int(origin_time[:2]),
            minute=int(origin_time[2:4]),
            second=int(origin_time[4:]),
            microsecond=0,
        )
        segment_at = fallback.replace(
            month=int(raw_date[:2]),
            day=int(raw_date[2:]),
            hour=int(effective_time[:2]),
            minute=int(effective_time[2:4]),
            second=int(effective_time[4:]),
            microsecond=0,
        )
    except ValueError:
        return None
    if segment_time and segment_at < origin_at:
        segment_at += timedelta(days=1)
    return segment_at


def _recording_origin_label_for_name(recording: Recording) -> str:
    fallback = _recording_local_datetime(recording)
    for candidate in _recording_name_candidates(recording):
        match = _RECORDING_NAME_START_PATTERN.search(Path(candidate).stem)
        if match is not None:
            raw_date = match.group("date")
            raw_time = match.group("time")
            return f"{raw_date}_{raw_time}"

    return fallback.strftime("%m%d_%H%M%S")


def _recording_segment_start_datetime_for_name(recording: Recording) -> datetime:
    fallback = _recording_local_datetime(recording)
    for candidate in _recording_name_candidates(recording):
        match = _RECORDING_NAME_START_PATTERN.search(Path(candidate).stem)
        if match is None:
            continue
        if recording.split_parent_recording_id and match.group("segment_time") is None:
            continue
        matched_at = _datetime_from_recording_name_match(match, fallback)
        if matched_at is not None:
            return matched_at
    return fallback


def _split_part_file_name(recording: Recording, split_at_ms: int, part_index: int) -> str:
    suffix = Path(recording.file_name or "").suffix or ".mp3"
    origin_label = _recording_origin_label_for_name(recording)
    start_at = _recording_segment_start_datetime_for_name(recording)
    if part_index == 2:
        start_at = start_at + timedelta(milliseconds=split_at_ms)
    time_label = start_at.strftime("%H%M%S")
    return f"{origin_label}_{time_label}{suffix}"


def _split_part_status(transcript_part: SplitTranscriptPart | None) -> str:
    return "transcribed" if transcript_part and (transcript_part.utterances or transcript_part.full_text) else "uploaded"


def _split_part_created_at(recording: Recording, split_at_ms: int, part_index: int) -> datetime:
    base = recording.created_at or datetime.now(timezone.utc)
    if part_index == 2:
        return base + timedelta(milliseconds=split_at_ms)
    return base


def _recording_transcript_utterances(recording: Recording) -> list:
    if recording.transcript and isinstance(recording.transcript.utterances, list):
        return recording.transcript.utterances
    if isinstance(recording.transcript_segments, list):
        return recording.transcript_segments
    if isinstance(recording.transcript_segments, dict) and isinstance(recording.transcript_segments.get("utterances"), list):
        return recording.transcript_segments["utterances"]
    return []


async def _create_split_transcript(
    db: AsyncSession,
    recording: Recording,
    transcript_part: SplitTranscriptPart | None,
    *,
    parent_transcript: Transcript | None,
) -> None:
    if transcript_part is None or not (transcript_part.utterances or transcript_part.full_text):
        return
    completed_at = datetime.now(timezone.utc)
    transcript = Transcript(
        recording_id=recording.id,
        asr_provider=f"split:{parent_transcript.asr_provider}" if parent_transcript else "split",
        asr_task_id=parent_transcript.asr_task_id if parent_transcript else None,
        status="completed",
        full_text=transcript_part.full_text,
        utterances=transcript_part.utterances,
        duration_ms=transcript_part.duration_ms,
        completed_at=completed_at,
    )
    recording.transcript_text = transcript_part.full_text
    recording.transcript_segments = transcript_part.utterances
    db.add(transcript)


def _staff_manifest_payload(recording: Recording) -> dict[str, str | None]:
    staff = recording.staff
    return {
        "staff_id": recording.staff_id,
        "staff_name": staff.name if staff else None,
        "staff_role": staff.role if staff else None,
        "staff_hospital_code": staff.hospital_code if staff else None,
        "staff_hospital_short_name": staff.hospital_short_name if staff else None,
    }


def _device_code_for_manifest(recording: Recording) -> str | None:
    if recording.device_id:
        return str(recording.device_id)
    if recording.staff and recording.staff.badge_id:
        return str(recording.staff.badge_id)
    return None


def _archive_item_sort_value(item: dict[str, object]) -> float:
    for key in ("create_time", "downloaded_at", "updated_at"):
        resolved = _archive_coerce_datetime(item.get(key))
        if resolved is not None:
            return resolved.timestamp()
    return 0.0


def _archive_item_recorded_date(item: dict[str, object]) -> date | None:
    for key in ("create_time", "downloaded_at", "updated_at"):
        resolved = _archive_coerce_datetime(item.get(key))
        if resolved is not None:
            return resolved.date()
    return None


def _archive_item_grouped_link_state_rank(item: dict[str, object]) -> int:
    current_status = str(item.get("pipeline_status") or "").strip().lower()
    if not item.get("has_visit_link") and current_status != "filtered":
        return 2
    if item.get("has_visit_link"):
        return 1
    return 0


def _build_archive_date_summaries(items: list[dict[str, object]]) -> list[ArchiveRecordingDateSummaryOut]:
    summary_by_date: dict[str | None, dict[str, int]] = {}
    for item in items:
        recorded_date = _archive_item_recorded_date(item)
        key = recorded_date.isoformat() if recorded_date is not None else None
        bucket = summary_by_date.setdefault(
            key,
            {
                "total": 0,
                "linked_count": 0,
                "needs_link_count": 0,
            },
        )
        bucket["total"] += 1
        if item.get("has_visit_link"):
            bucket["linked_count"] += 1
        else:
            bucket["needs_link_count"] += 1

    def sort_key(entry: tuple[str | None, dict[str, int]]) -> tuple[int, str]:
        key, _bucket = entry
        return (0 if key is None else 1, key or "")

    return [
        ArchiveRecordingDateSummaryOut(
            date=key,
            total=bucket["total"],
            linked_count=bucket["linked_count"],
            needs_link_count=bucket["needs_link_count"],
        )
        for key, bucket in sorted(summary_by_date.items(), key=sort_key, reverse=True)
    ]


def _filter_recording_match_result_visit_ids(
    result: RecordingVisitOrderMatchOut,
    accessible_visit_ids: set[str],
) -> RecordingVisitOrderMatchOut:
    result.linked_visit_ids = [visit_id for visit_id in result.linked_visit_ids if visit_id in accessible_visit_ids]
    if result.linked_visit_id not in accessible_visit_ids:
        result.linked_visit_id = None
    for candidate in result.candidates:
        if candidate.local_visit_id not in accessible_visit_ids:
            candidate.local_visit_id = None
        candidate.associated_local_visit_ids = [
            visit_id
            for visit_id in candidate.associated_local_visit_ids
            if visit_id in accessible_visit_ids
        ]
    return result


async def _archive_managed_staff_ids_for_user(db: AsyncSession | None, user: User) -> set[str] | None:
    role = normalize_permission_role(user.role)
    staff_id = _archive_clean_text(user.staff_id)
    if role == "super_admin" and not staff_id:
        return None
    if not staff_id:
        return set()
    if db is None:
        return {staff_id}

    actor_level = permission_role_level(user.role)
    rows = (
        await db.execute(
            select(Staff.id, Staff.permission_role)
            .join(StaffManagementRelation, StaffManagementRelation.subordinate_staff_id == Staff.id)
            .where(
                StaffManagementRelation.manager_staff_id == staff_id,
                Staff.is_active.is_(True),
            )
        )
    ).all()
    visible_staff_ids = {staff_id}
    for subordinate_staff_id, subordinate_role in rows:
        if role == "super_admin" or subordinate_staff_id == staff_id or permission_role_level(subordinate_role) <= actor_level:
            visible_staff_ids.add(subordinate_staff_id)
    return visible_staff_ids


def _archive_item_staff_id(item: dict[str, object]) -> str | None:
    return _archive_clean_text(item.get("staff_id"))


def _archive_item_visible_to_staff_ids(item: dict[str, object], visible_staff_ids: set[str] | None) -> bool:
    if visible_staff_ids is None:
        return True
    item_staff_id = _archive_item_staff_id(item)
    return bool(item_staff_id and item_staff_id in visible_staff_ids)


def clear_archive_recording_list_cache() -> None:
    _archive_recording_list_cache["expires_at"] = 0.0
    _archive_recording_list_cache["source_key"] = None
    _archive_recording_list_cache["items"] = None


async def _load_archive_recording_list_items(db: AsyncSession) -> list[dict[str, object]]:
    now = time.monotonic()
    source_key = id(_load_archive_recording_index)
    cached_items = _archive_recording_list_cache.get("items")
    cached_expires_at = float(_archive_recording_list_cache.get("expires_at") or 0.0)
    if (
        cached_items is not None
        and cached_expires_at > now
        and _archive_recording_list_cache.get("source_key") == source_key
    ):
        # Return a shallow list view of cached dicts. The list endpoint reads
        # items but does not mutate them in place (the rare include_analysis_summary
        # path makes per-item copies before writing).
        return list(cached_items)  # type: ignore[arg-type]

    # Single-flight: avoid stampede when many concurrent requests hit a cold cache.
    async with _archive_recording_list_lock:
        now = time.monotonic()
        cached_items = _archive_recording_list_cache.get("items")
        cached_expires_at = float(_archive_recording_list_cache.get("expires_at") or 0.0)
        if (
            cached_items is not None
            and cached_expires_at > now
            and _archive_recording_list_cache.get("source_key") == source_key
        ):
            return list(cached_items)  # type: ignore[arg-type]

        archive_index = _load_archive_recording_index()
        items = [payload["summary"] for payload in archive_index.values()]
        items = await _attach_archive_recording_bindings(db, items)
        # No deepcopy: items are freshly built each cache miss and not mutated
        # by the read path. The mutation site (include_analysis_summary) copies
        # the affected page_items before writing.
        _archive_recording_list_cache["items"] = items
        _archive_recording_list_cache["source_key"] = source_key
        _archive_recording_list_cache["expires_at"] = now + _ARCHIVE_RECORDING_LIST_CACHE_TTL_SECONDS
        return list(items)


@router.get("/archive", response_model=ArchiveRecordingPageOut)
async def list_archive_recordings(
    visit_id: str | None = Query(None),
    staff_id: str | None = Query(None),
    hospital_code: str | None = Query(None),
    status: str | None = Query(None),
    keyword: str | None = Query(None),
    link_state: str | None = Query(None),
    sort_mode: str | None = Query(None),
    exclude_filtered: bool = Query(False),
    exclude_quality_filtered: bool = Query(False),
    problem_only: bool = Query(False),
    include_date_summaries: bool = Query(True),
    include_analysis_summary: bool = Query(False),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items = await _load_archive_recording_list_items(db)

    normalized_status = _archive_clean_text(status)
    normalized_keyword = _archive_clean_text(keyword)
    normalized_link_state = _archive_clean_text(link_state)
    normalized_sort_mode = _archive_clean_text(sort_mode)
    normalized_visit_id = _archive_clean_text(visit_id)
    requested_staff_id = _archive_clean_text(staff_id)
    requested_hospital_code = _archive_clean_text(hospital_code)
    visible_staff_ids = await _archive_managed_staff_ids_for_user(db, current_user)
    exclude_filtered_enabled = exclude_filtered is True
    exclude_quality_filtered_enabled = exclude_quality_filtered is True
    problem_only_enabled = problem_only is True

    def matches(item: dict[str, object]) -> bool:
        if not _archive_item_visible_to_staff_ids(item, visible_staff_ids):
            return False
        if requested_staff_id and _archive_item_staff_id(item) != requested_staff_id:
            return False
        if requested_hospital_code and archive_item_hospital_code(item) != requested_hospital_code:
            return False
        if normalized_status and str(item.get("pipeline_status") or "").strip().lower() != normalized_status.lower():
            return False
        current_status = str(item.get("pipeline_status") or "").strip().lower()
        if exclude_filtered_enabled and current_status in {"filtered", "failed"}:
            return False
        if exclude_quality_filtered_enabled and current_status == "filtered":
            return False
        if problem_only_enabled and current_status not in {"filtered", "failed"}:
            return False
        if normalized_link_state == "linked" and not item.get("has_visit_link"):
            return False
        if normalized_link_state == "unlinked" and item.get("has_visit_link"):
            return False
        if normalized_link_state == "needs_link" and not item.get("needs_visit_link"):
            return False
        if normalized_visit_id:
            linked_visit_ids = {str(value) for value in (item.get("linked_visit_ids") or [])}
            if str(item.get("visit_id") or "") != normalized_visit_id and normalized_visit_id not in linked_visit_ids:
                return False
        recorded_date = _archive_item_recorded_date(item)
        if date_from and (recorded_date is None or recorded_date < date_from):
            return False
        if date_to and (recorded_date is None or recorded_date > date_to):
            return False
        if normalized_keyword:
            haystack = " ".join(
                str(value or "")
                for value in (
                    item.get("display_file_name"),
                    item.get("archive_file_name"),
                    item.get("staged_file_name"),
                    item.get("remote_file_name"),
                    item.get("file_id"),
                    item.get("sn"),
                    item.get("device_code"),
                    item.get("staff_name"),
                    item.get("stage_key"),
                    " ".join(str(value or "") for value in (item.get("linked_visit_order_refs") or [])),
                    " ".join(str(value or "") for value in (item.get("linked_customer_names") or [])),
                )
            ).lower()
            if normalized_keyword.lower() not in haystack:
                return False
        return True

    filtered = [item for item in items if matches(item)]
    # Only the generic workbench views should bubble up "needs visit link" items.
    # Explicit status views such as the analysis page (`status=analyzed`) should
    # remain purely time-ordered, otherwise recent analyzed recordings that are
    # already linked get buried behind older pending items.
    prioritize_pending = bool(
        not normalized_status
        and (
            normalized_link_state
            or exclude_filtered_enabled
            or exclude_quality_filtered_enabled
            or problem_only_enabled
        )
    )
    if normalized_sort_mode == "date_grouped_link_state":
        def grouped_sort_key(item: dict[str, object]) -> tuple[int, int, float]:
            recorded_date = _archive_item_recorded_date(item)
            return (
                recorded_date.toordinal() if recorded_date is not None else 0,
                _archive_item_grouped_link_state_rank(item),
                _archive_item_sort_value(item),
            )

        filtered.sort(key=grouped_sort_key, reverse=True)
    elif prioritize_pending:
        filtered.sort(
            key=lambda item: (
                1 if item.get("needs_visit_link") else 0,
                1 if str(item.get("pipeline_status") or "") not in {"filtered", "failed"} else 0,
                _archive_item_sort_value(item),
            ),
            reverse=True,
        )
    else:
        filtered.sort(key=_archive_item_sort_value, reverse=True)

    total = len(filtered)
    date_summaries = _build_archive_date_summaries(filtered) if include_date_summaries else []
    start = (page - 1) * page_size
    end = start + page_size
    page_items = filtered[start:end]
    if include_analysis_summary:
        # Copy items before mutating to avoid contaminating the shared cache.
        page_items = [dict(item) for item in page_items]
        for item in page_items:
            latest_analysis = item.get("_latest_analysis_result")
            if isinstance(latest_analysis, dict):
                item["analysis_summary"] = _build_archive_analysis_summary(item, None, latest_analysis)
    page_response = make_page_response(
        [ArchiveRecordingOut.model_validate(item) for item in page_items],
        total,
        page,
        page_size,
    )
    return ArchiveRecordingPageOut(
        items=page_response.items,
        total=page_response.total,
        page=page_response.page,
        page_size=page_response.page_size,
        pages=page_response.pages,
        date_summaries=date_summaries,
    )


@router.get("/archive/{item_id}", response_model=ArchiveRecordingDetailOut)
async def get_archive_recording_detail(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    archive_index = _load_archive_recording_index()
    payload = archive_index.get(item_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Archive recording not found")

    summary = dict(payload["summary"])
    [summary] = await _attach_archive_recording_bindings(db, [summary])
    visible_staff_ids = await _archive_managed_staff_ids_for_user(db, current_user)
    if not _archive_item_visible_to_staff_ids(summary, visible_staff_ids):
        raise HTTPException(status_code=404, detail="Archive recording not found")

    manifest = payload.get("manifest")
    archive_metadata = payload.get("archive_metadata")

    transcript = await _resolve_archive_transcript(db, summary=summary, manifest=manifest)

    analysis_result = await _resolve_archive_analysis_result(
        db,
        summary=summary,
        manifest=manifest,
    )

    return ArchiveRecordingDetailOut.model_validate(
        {
            **summary,
            "manifest": manifest,
            "archive_metadata": archive_metadata,
            "transcript": transcript,
            "analysis_result": analysis_result,
            "analysis_summary": _build_archive_analysis_summary(summary, transcript, analysis_result),
        }
    )


@router.post("/archive/{item_id}/ensure-recording", response_model=ArchiveRecordingEnsureOut)
async def ensure_archive_recording(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    archive_index = _load_archive_recording_index()
    payload = archive_index.get(item_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Archive recording not found")

    summary = dict(payload["summary"])
    [summary] = await _attach_archive_recording_bindings(db, [summary])
    visible_staff_ids = await _archive_managed_staff_ids_for_user(db, current_user)
    if not _archive_item_visible_to_staff_ids(summary, visible_staff_ids):
        raise HTTPException(status_code=404, detail="Archive recording not found")

    recording, created = await _ensure_archive_recording_entry(db, item_id=item_id)
    binding = {
        "recording_id": recording.id,
        "file_name": recording.file_name,
        "display_file_name": summary.get("display_file_name") or recording.file_name,
        "visit_id": recording.visit_id,
        "linked_visit_ids": summary.get("linked_visit_ids") or [],
        "linked_visit_order_refs": summary.get("linked_visit_order_refs") or [],
        "linked_customer_names": summary.get("linked_customer_names") or [],
    }
    return ArchiveRecordingEnsureOut.model_validate(
        {
            "item_id": item_id,
            "created_new_recording": created,
            **binding,
        }
    )


@router.get("/archive/{item_id}/media")
async def get_archive_recording_media(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    archive_index = _load_archive_recording_index()
    payload = archive_index.get(item_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Archive recording not found")

    summary = dict(payload["summary"])
    [summary] = await _attach_archive_recording_bindings(db, [summary])
    visible_staff_ids = await _archive_managed_staff_ids_for_user(db, current_user)
    if not _archive_item_visible_to_staff_ids(summary, visible_staff_ids):
        raise HTTPException(status_code=404, detail="Archive recording not found")

    audio_path = _resolve_archive_recording_audio_path(
        payload.get("archive_metadata"),
        payload.get("manifest"),
    )
    if audio_path is None or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Archive audio file not found")

    media_type, _ = mimetypes.guess_type(audio_path.name)
    return FileResponse(
        path=audio_path,
        media_type=media_type or "application/octet-stream",
        filename=audio_path.name,
    )


@router.get("/archive/{item_id}/media-url", response_model=RecordingMediaSourceOut)
async def get_archive_recording_media_url(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    archive_index = _load_archive_recording_index()
    payload = archive_index.get(item_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Archive recording not found")

    summary = dict(payload["summary"])
    [summary] = await _attach_archive_recording_bindings(db, [summary])
    visible_staff_ids = await _archive_managed_staff_ids_for_user(db, current_user)
    if not _archive_item_visible_to_staff_ids(summary, visible_staff_ids):
        raise HTTPException(status_code=404, detail="Archive recording not found")

    audio_path = _resolve_archive_recording_audio_path(
        payload.get("archive_metadata"),
        payload.get("manifest"),
    )
    if audio_path is None or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Archive audio file not found")

    media_type, _ = mimetypes.guess_type(audio_path.name)
    return RecordingMediaSourceOut(
        url=build_tencent_media_path(audio_path, filename=audio_path.name),
        file_name=audio_path.name,
        media_type=media_type,
    )


@router.get("", response_model=PaginatedResponse[RecordingOut])
async def list_recordings(
    visit_id: str | None = Query(None),
    staff_id: str | None = Query(None),
    status: str | None = Query(None),
    keyword: str | None = Query(None),
    customer_keyword: str | None = Query(None),
    badge_id: str | None = Query(None),
    role: str | None = Query(None),
    has_visit: bool | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    scope = await build_permission_scope(current_user)
    _legacy_prefix_filter = ~Recording.file_name.regexp_match(r'^audio_\d+$')
    stmt = (
        select(Recording)
        .outerjoin(Staff, Recording.staff_id == Staff.id)
        .outerjoin(Device, Recording.device_id == Device.id)
        .outerjoin(Visit, Recording.visit_id == Visit.id)
        .outerjoin(Customer, Visit.customer_id == Customer.id)
        .where(recording_scope_condition(scope))
        .where(_legacy_prefix_filter)
        .order_by(Recording.created_at.desc())
    )
    count_stmt = (
        select(func.count(func.distinct(Recording.id)))
        .select_from(Recording)
        .outerjoin(Staff, Recording.staff_id == Staff.id)
        .outerjoin(Device, Recording.device_id == Device.id)
        .outerjoin(Visit, Recording.visit_id == Visit.id)
        .outerjoin(Customer, Visit.customer_id == Customer.id)
        .where(recording_scope_condition(scope))
        .where(_legacy_prefix_filter)
    )
    if visit_id:
        like = f"%{visit_id.strip()}%"
        visit_filter = (
            select(RecordingVisitLink.id)
            .where(
                RecordingVisitLink.recording_id == Recording.id,
                cast(RecordingVisitLink.visit_id, String).ilike(like),
            )
            .exists()
        )
        stmt = stmt.where(visit_filter)
        count_stmt = count_stmt.where(visit_filter)
    if staff_id:
        stmt = stmt.where(Recording.staff_id == staff_id)
        count_stmt = count_stmt.where(Recording.staff_id == staff_id)
    if status:
        stmt = stmt.where(Recording.status == status)
        count_stmt = count_stmt.where(Recording.status == status)
    else:
        stmt = stmt.where(Recording.status != "filtered")
        count_stmt = count_stmt.where(Recording.status != "filtered")
    if keyword:
        like = f"%{keyword.strip()}%"
        keyword_filter = (
            or_(
                Recording.file_name.ilike(like),
                cast(Recording.id, String).ilike(like),
                cast(Recording.device_id, String).ilike(like),
                Device.device_code.ilike(like),
            )
        )
        stmt = stmt.where(keyword_filter)
        count_stmt = count_stmt.where(keyword_filter)
    if customer_keyword:
        like = f"%{customer_keyword.strip()}%"
        linked_visit = aliased(Visit)
        linked_customer = aliased(Customer)
        linked_customer_filter = (
            select(RecordingVisitLink.id)
            .join(linked_visit, linked_visit.id == RecordingVisitLink.visit_id)
            .join(linked_customer, linked_customer.id == linked_visit.customer_id)
            .where(
                RecordingVisitLink.recording_id == Recording.id,
                linked_customer.name.ilike(like),
            )
            .exists()
        )
        customer_filter = or_(Customer.name.ilike(like), linked_customer_filter)
        stmt = stmt.where(customer_filter)
        count_stmt = count_stmt.where(customer_filter)
    if badge_id:
        like = f"%{badge_id.strip()}%"
        badge_filter = or_(Staff.badge_id.ilike(like), Device.device_code.ilike(like))
        stmt = stmt.where(badge_filter)
        count_stmt = count_stmt.where(badge_filter)
    if role:
        stmt = stmt.where(Staff.role == role)
        count_stmt = count_stmt.where(Staff.role == role)
    if has_visit is True:
        stmt = stmt.where(Recording.visit_id.is_not(None))
        count_stmt = count_stmt.where(Recording.visit_id.is_not(None))
    if has_visit is False:
        stmt = stmt.where(Recording.visit_id.is_(None))
        count_stmt = count_stmt.where(Recording.visit_id.is_(None))
    if date_from:
        # 直接走索引：created_at >= 当日 00:00 UTC
        start_dt = datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc)
        stmt = stmt.where(Recording.created_at >= start_dt)
        count_stmt = count_stmt.where(Recording.created_at >= start_dt)
    if date_to:
        end_dt = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        stmt = stmt.where(Recording.created_at < end_dt)
        count_stmt = count_stmt.where(Recording.created_at < end_dt)
    total: int = (await db.execute(count_stmt)).scalar_one()
    rows = (
        await db.execute(stmt.options(*_load_opts()).offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    device_ids = {row.device_id for row in rows if row.device_id}
    device_code_map: dict[str, str] = {}
    if device_ids:
        device_code_rows = await db.execute(select(Device.id, Device.device_code).where(Device.id.in_(device_ids)))
        device_code_map = {device_id: device_code for device_id, device_code in device_code_rows.all()}
    return make_page_response([_to_out(row, device_code_map.get(row.device_id or '')) for row in rows], total, page, page_size)


@router.get("/{recording_id}", response_model=RecordingOut)
async def get_recording(
    recording_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")
    device_code = None
    if recording.device_id:
        device_code = (await db.execute(select(Device.device_code).where(Device.id == recording.device_id))).scalar_one_or_none()
    return _to_out(recording, device_code)


@router.post("/{recording_id}/split", response_model=RecordingSplitOut)
async def split_recording(
    recording_id: str,
    body: RecordingSplitRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")
    if not _can_split_recording(recording, current_user):
        raise HTTPException(403, "只有录音上传者或管理员可以裁切录音")
    if not body.confirm:
        raise HTTPException(400, "裁切录音前需要二次确认")
    if recording.status == "filtered":
        raise HTTPException(400, "已过滤或已裁切的原始录音不能再次裁切")

    split_at_ms = body.resolved_split_at_ms()
    duration_ms = _recording_duration_ms(recording)
    min_edge_ms = 1000
    if split_at_ms < min_edge_ms:
        raise HTTPException(400, "裁切时间点至少需要晚于录音开始 1 秒")
    if duration_ms is not None and split_at_ms > duration_ms - min_edge_ms:
        raise HTTPException(400, "裁切时间点至少需要早于录音结束 1 秒")

    settings = get_settings()
    source_path = settings.resolve_file_path(recording.file_path)
    if not source_path.is_file():
        raise HTTPException(404, "Recording file not found")
    ext = source_path.suffix.lower() or Path(recording.file_name).suffix.lower() or ".mp3"
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file format: {ext}")

    part_ids = [_new_id(), _new_id()]
    split_dir = _ensure_upload_dir() / "splits" / recording.id
    output_paths = [split_dir / f"{part_ids[0]}{ext}", split_dir / f"{part_ids[1]}{ext}"]
    try:
        await asyncio.to_thread(
            split_audio_file,
            source_path,
            output_paths[0],
            output_paths[1],
            split_at_ms=split_at_ms,
        )
    except RecordingSplitError as exc:
        raise HTTPException(422, str(exc)) from exc

    utterances = _recording_transcript_utterances(recording)
    transcript_parts: tuple[SplitTranscriptPart | None, SplitTranscriptPart | None]
    if utterances:
        transcript_parts = split_transcript_utterances(
            utterances,
            split_at_ms=split_at_ms,
            total_duration_ms=duration_ms,
        )
    else:
        transcript_parts = (None, None)

    original_linked_visit_ids = [
        link.visit_id
        for link in ordered_recording_visit_links(recording)
        if str(link.visit_id or "").strip()
    ]
    now = datetime.now(timezone.utc)
    staff_payload = _staff_manifest_payload(recording)
    device_code = _device_code_for_manifest(recording)
    created_parts: list[Recording] = []
    archive_item_ids: dict[str, str | None] = {}

    try:
        for index, (part_id, output_path, transcript_part) in enumerate(
            zip(part_ids, output_paths, transcript_parts, strict=True),
            start=1,
        ):
            part_duration_ms = (
                split_at_ms
                if index == 1
                else (duration_ms - split_at_ms if duration_ms is not None and duration_ms > split_at_ms else None)
            )
            duration_seconds = max(1, int(round(part_duration_ms / 1000))) if part_duration_ms else None
            part = Recording(
                id=part_id,
                visit_id=None,
                staff_id=recording.staff_id,
                device_id=recording.device_id,
                file_name=_split_part_file_name(recording, split_at_ms, index),
                file_path=settings.make_relative_path(output_path.resolve()),
                file_size=output_path.stat().st_size,
                duration_seconds=duration_seconds,
                status=_split_part_status(transcript_part),
                split_parent_recording_id=recording.id,
                split_part_index=index,
                split_at_ms=split_at_ms,
                created_at=_split_part_created_at(recording, split_at_ms, index),
                updated_at=now,
            )
            db.add(part)
            created_parts.append(part)

        recording.status = "filtered"
        recording.visit_id = None
        recording.updated_at = now
        recording.visit_links.clear()
        await db.flush()

        for part, transcript_part in zip(created_parts, transcript_parts, strict=True):
            await _create_split_transcript(
                db,
                part,
                transcript_part,
                parent_transcript=recording.transcript,
            )

        for part, output_path, transcript_part in zip(created_parts, output_paths, transcript_parts, strict=True):
            archive_item_ids[part.id] = write_split_archive_manifest(
                recording_id=part.id,
                parent_recording_id=recording.id,
                part_index=part.split_part_index or 0,
                split_at_ms=split_at_ms,
                file_name=part.file_name,
                audio_path=output_path.resolve(),
                file_size=part.file_size,
                duration_ms=(part.duration_seconds * 1000 if part.duration_seconds else None),
                duration_seconds=part.duration_seconds,
                status=part.status,
                created_at=part.created_at,
                device_code=device_code,
                device_id=recording.device_id,
                transcript=transcript_part,
                **staff_payload,
            )

        await db.commit()
    except Exception:
        await db.rollback()
        for path in output_paths:
            path.unlink(missing_ok=True)
        raise

    if original_linked_visit_ids:
        customer_ids = (
            await db.execute(select(Visit.customer_id).where(Visit.id.in_(original_linked_visit_ids)))
        ).scalars().all()
        for customer_id in {str(item or "").strip() for item in customer_ids if str(item or "").strip()}:
            await refresh_customer_profile_scores(db, customer_id)
        await db.commit()

    stored_parts = (
        await db.execute(
            select(Recording)
            .where(Recording.id.in_([part.id for part in created_parts]))
            .options(*_load_opts())
            .order_by(Recording.split_part_index.asc())
        )
    ).scalars().unique().all()
    return RecordingSplitOut(
        original_recording_id=recording.id,
        split_at_ms=split_at_ms,
        parts=[
            RecordingSplitPartOut(
                part_index=part.split_part_index or index,
                archive_item_id=archive_item_ids.get(part.id),
                recording=_to_out(part),
            )
            for index, part in enumerate(stored_parts, start=1)
        ],
        message="录音已裁切为 2 段，原录音已隐藏，新片段可按实际客户重新关联到诊单。",
    )


@router.get("/{recording_id}/visit-order-match", response_model=RecordingVisitOrderMatchOut)
async def get_recording_visit_order_match(
    recording_id: str,
    apply_auto: bool = Query(True),
    use_llm: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")
    scope = await build_permission_scope(current_user)
    result = await analyze_recording_visit_order_match(
        db,
        recording_id,
        apply_auto=apply_auto,
        use_llm=use_llm,
        scope=scope,
    )
    if result is None:
        raise HTTPException(404, "Recording not found")
    visit_ids = {
        visit_id
        for visit_id in [
            result.linked_visit_id,
            *result.linked_visit_ids,
            *[candidate.local_visit_id for candidate in result.candidates],
            *[visit_id for candidate in result.candidates for visit_id in candidate.associated_local_visit_ids],
        ]
        if str(visit_id or "").strip()
    }
    if visit_ids:
        accessible_visit_ids = set(
            (
                await db.execute(
                    select(Visit.id).where(
                        Visit.id.in_(visit_ids),
                        visit_scope_condition(scope),
                    )
                )
            ).scalars().all()
        )
        result = _filter_recording_match_result_visit_ids(result, accessible_visit_ids)
    return result


@router.post("/{recording_id}/visit-order-local-visit", response_model=RecordingVisitOrderLocalVisitOut)
async def ensure_recording_visit_order_local_visit(
    recording_id: str,
    body: RecordingVisitOrderLocalVisitRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")
    visit_order = await _get_linkable_visit_order_for_recording(
        db,
        current_user=current_user,
        recording=recording,
        visit_order_id=body.visit_order_id,
    )
    visit = await _ensure_local_visit_for_visit_order(db, visit_order)
    await db.commit()
    return RecordingVisitOrderLocalVisitOut(
        visit_id=visit.id,
        visit_order_id=visit_order.id,
        dzdh=visit_order.dzdh,
        dzseg=visit_order.dzseg,
    )


@router.get("/{recording_id}/media")
async def get_recording_media(
    recording_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")

    file_path = get_settings().resolve_file_path(recording.file_path)
    if not file_path.is_file():
        raise HTTPException(404, "Recording file not found")

    media_type, _ = mimetypes.guess_type(recording.file_name)
    return FileResponse(
        path=file_path,
        media_type=media_type or "application/octet-stream",
        filename=recording.file_name,
    )


@router.get("/{recording_id}/media-url", response_model=RecordingMediaSourceOut)
async def get_recording_media_url(
    recording_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")

    file_path = get_settings().resolve_file_path(recording.file_path)
    if not file_path.is_file():
        raise HTTPException(404, "Recording file not found")

    media_type, _ = mimetypes.guess_type(recording.file_name)
    return RecordingMediaSourceOut(
        url=build_tencent_media_path(file_path, filename=recording.file_name),
        file_name=recording.file_name,
        media_type=media_type,
    )


@router.get("/{recording_id}/analysis", response_model=TaskDetailOut | None)
async def get_recording_analysis(
    recording_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")

    analysis_file_name = f"recording_{recording_id}.json"
    task = (
        await db.execute(
            select(AnalysisTask)
            .where(AnalysisTask.file_name == analysis_file_name)
            .order_by(AnalysisTask.created_at.desc())
        )
    ).scalars().first()
    return normalize_task_detail(task) if task else None


@router.get("/{recording_id}/multi-customer-review", response_model=RecordingMultiCustomerReviewOut)
async def get_recording_multi_customer_review(
    recording_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")

    recording = await ensure_multi_customer_review(db, recording_id)
    if recording is None:
        raise HTTPException(404, "Recording not found")
    await sync_visit_analysis_results(db, recording_id)
    await db.commit()
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")
    return _serialize_multi_customer_review(recording)


@router.post("/{recording_id}/multi-customer-review/confirm", response_model=RecordingMultiCustomerReviewOut)
async def confirm_recording_multi_customer_review(
    recording_id: str,
    body: RecordingMultiCustomerConfirmRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")
    try:
        recording = await confirm_multi_customer_mappings(
            db,
            recording_id,
            [item.model_dump() for item in body.mappings],
            confirmed_by_user=current_user,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _serialize_multi_customer_review(recording)


@router.post("/{recording_id}/multi-customer-review/reset", response_model=RecordingMultiCustomerReviewOut)
async def reset_recording_multi_customer_review(
    recording_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")
    try:
        recording = await reset_multi_customer_mappings(
            db,
            recording_id,
            reset_by_user=current_user,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _serialize_multi_customer_review(recording)


def _raise_sap_preview_error(result: dict) -> None:
    if "error" not in result:
        return
    status_map = {
        "recording_not_found": 404,
        "visit_order_not_found": 404,
        "no_visit_linked": 422,
        "no_visit_order": 422,
        "no_analysis": 422,
        "multi_customer_review_required": 422,
        "multi_customer_analysis_pending": 422,
    }
    raise HTTPException(
        status_code=status_map.get(result["error"], 400),
        detail=result["message"],
    )


async def _dispatch_created_sap_push_log(push_log, *, db: AsyncSession) -> tuple[bool, str]:
    settings = get_settings()
    if push_log.status == "queued":
        await dispatch_sap_push_log(push_log.id)
        return True, "SAP RFC 回传任务已入队，稍后由后台执行。"
    if push_log.status == "prepared":
        await execute_sap_push_log(push_log.id)
        await db.refresh(push_log)
        return False, "SAP RFC 回传已执行成功。" if push_log.status == "succeeded" else "SAP RFC 回传已执行，但返回失败，请查看日志详情。"
    return False, "SAP RFC 回传当前处于关闭状态，已保存预备日志但未发送外部请求。" if not settings.sap_rfc_send_enabled else "SAP RFC 回传日志已创建。"


async def _maybe_auto_dispatch_sap_push(
    db: AsyncSession,
    recording_id: str,
    *,
    initiated_by: str | None,
) -> None:
    settings = get_settings()
    if settings.sap_rfc_auto_push_on_bind:
        logger.info(
            "sap auto push deferred to stable-bind scheduler recording_id=%s initiated_by=%s",
            recording_id,
            initiated_by or "",
        )
    return


@router.post("/{recording_id}/push-sap", response_model=SapPushPreviewOut)
async def push_recording_to_sap(
    recording_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """生成 SAP 咨询单回传数据。单条请求体内的 TAB_SYZ 可包含多条适应症。"""
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")
    result = await generate_sap_consultation_payloads(db, recording_id)
    _raise_sap_preview_error(result)
    return result


@router.get("/{recording_id}/push-sap/logs", response_model=list[SapPushLogOut])
async def list_recording_push_sap_logs(
    recording_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")
    logs = await list_recording_sap_push_logs(db, recording_id)
    return [serialize_sap_push_log(item) for item in logs]


@router.post("/{recording_id}/push-sap/dispatch", response_model=SapPushDispatchOut)
async def dispatch_recording_to_sap(
    recording_id: str,
    body: SapPushDispatchRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, user)
    if not recording:
        raise HTTPException(404, "Recording not found")
    if body.target_visit_id:
        await _ensure_visit_ids_in_scope(db, user, [body.target_visit_id])
    try:
        preview = await generate_sap_consultation_payloads(db, recording_id, target_visit_id=body.target_visit_id)
        _raise_sap_preview_error(preview)
        target_visit_ids = [
            str(target.get("visit_id") or "").strip()
            for target in preview.get("targets", [])
            if str(target.get("visit_id") or "").strip()
        ]
        if not body.target_visit_id and len(target_visit_ids) > 1:
            push_logs = []
            queued_any = False
            for target_visit_id in target_visit_ids:
                push_log = await create_sap_push_log(
                    db,
                    recording_id,
                    target_visit_id=target_visit_id,
                    trigger_mode=body.trigger_mode,
                    initiated_by=user.display_name or user.username,
                    prefer_async=body.async_dispatch,
                )
                queued, _message = await _dispatch_created_sap_push_log(push_log, db=db)
                queued_any = queued_any or queued
                push_logs.append(push_log)
            settings = get_settings()
            serialized_logs = [serialize_sap_push_log(item) for item in push_logs]
            return {
                "queued": queued_any,
                "dispatch_mode": settings.sap_rfc_dispatch_mode,
                "send_enabled": bool(settings.sap_rfc_send_enabled),
                "message": f"已按 {len(push_logs)} 张到诊单分别创建 SAP 回传日志。",
                "log": serialized_logs[0],
                "logs": serialized_logs,
            }

        push_log = await create_sap_push_log(
            db,
            recording_id,
            target_visit_id=body.target_visit_id,
            trigger_mode=body.trigger_mode,
            initiated_by=user.display_name or user.username,
            prefer_async=body.async_dispatch,
        )
    except SapPushPreparationError as exc:
        status_map = {
            "recording_not_found": 404,
            "visit_order_not_found": 404,
            "no_visit_linked": 422,
            "no_visit_order": 422,
            "no_analysis": 422,
            "multi_customer_review_required": 422,
            "multi_customer_analysis_pending": 422,
        }
        raise HTTPException(status_map.get(exc.error_code, 400), exc.message) from exc

    settings = get_settings()
    queued, message = await _dispatch_created_sap_push_log(push_log, db=db)

    return {
        "queued": queued,
        "dispatch_mode": settings.sap_rfc_dispatch_mode,
        "send_enabled": bool(settings.sap_rfc_send_enabled),
        "message": message,
        "log": serialize_sap_push_log(push_log),
        "logs": [serialize_sap_push_log(push_log)],
    }


@router.post("/upload", response_model=RecordingOut, status_code=201)
async def upload_recording(
    file: UploadFile,
    visit_id: str | None = Query(None),
    staff_id: str | None = Query(None),
    device_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not file.filename:
        raise HTTPException(400, "Missing file name")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file format: {ext}")

    if visit_id:
        await _ensure_visit_ids_in_scope(db, user, [visit_id])
    if staff_id and not await db.get(Staff, staff_id):
        raise HTTPException(400, "Staff not found")

    upload_dir = _ensure_upload_dir()
    file_id = _new_id()
    dest = upload_dir / f"{file_id}{ext}"

    content = await file.read()
    async with aiofiles.open(dest, "wb") as handle:
        await handle.write(content)

    recording = Recording(
        id=file_id,
        visit_id=None,
        staff_id=staff_id,
        device_id=device_id,
        file_name=file.filename,
        file_path=get_settings().make_relative_path(dest),
        file_size=dest.stat().st_size,
        status="uploaded",
    )
    db.add(recording)
    await db.flush()
    if visit_id:
        try:
            await sync_recording_visit_links(db, recording, [visit_id], primary_visit_id=visit_id, source="upload")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    await db.commit()
    await _sync_visit_orders_for_recording_context(db, recording)

    stored = (
        await db.execute(select(Recording).where(Recording.id == recording.id).options(*_load_opts()))
    ).scalar_one()
    if visit_id:
        await _maybe_auto_dispatch_sap_push(
            db,
            recording.id,
            initiated_by=user.display_name or user.username,
        )
    return _to_out(stored)


@router.put("/{recording_id}", response_model=RecordingOut)
async def update_recording(
    recording_id: str,
    body: RecordingUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    scope = await build_permission_scope(user)
    recording = (
        await db.execute(
            select(Recording)
            .where(Recording.id == recording_id, recording_scope_condition(scope))
            .options(selectinload(Recording.segments), selectinload(Recording.visit_links))
        )
    ).scalar_one_or_none()
    if not recording:
        raise HTTPException(404, "Recording not found")

    updates = body.model_dump(exclude_unset=True)
    if "staff_id" in updates and updates["staff_id"] and not await db.get(Staff, updates["staff_id"]):
        raise HTTPException(400, "Staff not found")

    visit_id_provided = "visit_id" in updates
    linked_visit_ids_provided = "linked_visit_ids" in updates
    requested_primary_visit_id = updates.pop("visit_id", None)
    requested_linked_visit_ids = updates.pop("linked_visit_ids", None)
    staff_context_changed = "staff_id" in updates

    for key, value in updates.items():
        setattr(recording, key, value)

    if visit_id_provided or linked_visit_ids_provided:
        if linked_visit_ids_provided:
            target_visit_ids = requested_linked_visit_ids or (
                [requested_primary_visit_id] if requested_primary_visit_id else []
            )
        elif requested_primary_visit_id:
            # Older UI entry points only send visit_id when adopting a match.
            # Treat that as "set primary and keep existing secondary links",
            # so adding a new association never erases companion/multi-recording
            # context unless the caller explicitly sends linked_visit_ids.
            existing_visit_ids = [link.visit_id for link in ordered_recording_visit_links(recording)]
            target_visit_ids = [requested_primary_visit_id, *existing_visit_ids]
        else:
            target_visit_ids = []
        await _ensure_visit_ids_in_scope(db, user, target_visit_ids, recording=recording)
        try:
            await sync_recording_visit_links(
                db,
                recording,
                target_visit_ids,
                primary_visit_id=requested_primary_visit_id,
                source="manual",
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    await db.commit()
    if staff_context_changed:
        await _sync_visit_orders_for_recording_context(db, recording)
    if (visit_id_provided or linked_visit_ids_provided) and recording.visit_id:
        await _refresh_customer_profile_scores_for_recording_links(db, recording_id)
        await ensure_multi_customer_review(db, recording.id)
        await db.commit()
        await _maybe_auto_dispatch_sap_push(
            db,
            recording.id,
            initiated_by=user.display_name or user.username,
        )

    stored = (
        await db.execute(
            select(Recording)
            .where(Recording.id == recording_id)
            .options(*_load_opts())
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return _to_out(stored)


@router.delete("/{recording_id}", status_code=204)
async def delete_recording(
    recording_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")

    file_path = get_settings().resolve_file_path(recording.file_path)
    if file_path.is_file():
        file_path.unlink(missing_ok=True)

    await db.delete(recording)
    await db.commit()


@router.post("/{recording_id}/analyze", response_model=TaskOut, status_code=201)
async def analyze_recording(
    recording_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")

    transcript = (
        await db.execute(select(Transcript).where(Transcript.recording_id == recording_id))
    ).scalar_one_or_none()
    if transcript is None or transcript.status != "completed":
        raise HTTPException(400, "Transcript is not ready")

    try:
        return await create_or_dispatch_recording_analysis(db, recording_id, transcript=transcript)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, "Task created but dispatch failed") from exc


class BatchImportItem(BaseModel):
    file_name: str
    recording_id: str
    status: str
    message: str


class BatchImportResult(BaseModel):
    imported: int
    skipped: int
    errors: int
    items: list[BatchImportItem]


@router.post("/batch-import", response_model=BatchImportResult)
async def batch_import_from_directory(
    directory: Annotated[str, Body(embed=True, description="Absolute path of the audio directory to scan")],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_system_admin_or_above),
):
    del current_user
    source_dir = Path(directory).resolve()
    if not source_dir.is_dir():
        raise HTTPException(400, f"Directory does not exist: {directory}")

    settings = get_settings()
    allowed_roots = settings.resolved_batch_import_allowed_paths
    if not allowed_roots:
        raise HTTPException(403, "批量导入未配置允许目录，请设置 BATCH_IMPORT_ALLOWED_DIRS")
    if not any(source_dir == root or root in source_dir.parents for root in allowed_roots):
        raise HTTPException(403, "Import from this directory is not allowed")

    existing_names: set[str] = set()
    audio_files = sorted(
        path for path in source_dir.iterdir() if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS
    )
    if not audio_files:
        raise HTTPException(400, f"No supported audio files found in: {directory}")

    # 仅检查待导入文件名是否已存在，避免拉全表 file_name 集合（可能成万行）。
    candidate_names = [path.name for path in audio_files]
    chunk_size = 500
    for start in range(0, len(candidate_names), chunk_size):
        chunk = candidate_names[start : start + chunk_size]
        rows = (
            await db.execute(
                select(Recording.file_name).where(Recording.file_name.in_(chunk))
            )
        ).scalars().all()
        existing_names.update(rows)

    upload_dir = _ensure_upload_dir()
    items: list[BatchImportItem] = []
    imported = 0
    skipped = 0
    errors = 0

    for audio_file in audio_files:
        if audio_file.name in existing_names:
            items.append(
                BatchImportItem(
                    file_name=audio_file.name,
                    recording_id="",
                    status="skipped",
                    message="A recording with the same file name already exists",
                )
            )
            skipped += 1
            continue

        try:
            file_id = _new_id()
            dest = upload_dir / f"{file_id}{audio_file.suffix.lower()}"

            async with aiofiles.open(audio_file, "rb") as src:
                content = await src.read()
            async with aiofiles.open(dest, "wb") as dst:
                await dst.write(content)

            recording = Recording(
                id=file_id,
                file_name=audio_file.name,
                file_path=settings.make_relative_path(dest),
                file_size=dest.stat().st_size,
                status="uploaded",
            )
            db.add(recording)
            await db.commit()

            items.append(
                BatchImportItem(
                    file_name=audio_file.name,
                    recording_id=file_id,
                    status="imported",
                    message="Imported successfully. Trigger ASR manually when needed.",
                )
            )
            imported += 1
            existing_names.add(audio_file.name)
        except Exception as exc:
            await db.rollback()
            items.append(
                BatchImportItem(
                    file_name=audio_file.name,
                    recording_id="",
                    status="error",
                    message=str(exc),
                )
            )
            errors += 1

    return BatchImportResult(imported=imported, skipped=skipped, errors=errors, items=items)
