from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import String, cast, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.analysis.reference_data import resolve_indication_reference_item
from smart_badge_api.api.deps import get_current_user, get_db
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import (
    AnalysisTask,
    Recording,
    RecordingVisitLink,
    SapConsultationReview,
    SapPushLog,
    Staff,
    User,
    Visit,
    VisitOrder,
)
from smart_badge_api.schemas.pagination import PaginatedResponse, make_page_response
from smart_badge_api.sap_consultation import (
    _build_recording_consultation_text_for_visit,
    _is_sap_summary_section_enabled,
    _load_sap_summary_template_config,
    _load_visit_recording_contexts,
    generate_sap_consultation_payloads,
)
from smart_badge_api.sap_push_service import (
    SapPushPreparationError,
    create_sap_push_log,
    execute_sap_push_log,
    serialize_sap_push_log,
    summarize_sap_push_log_result,
)
from smart_badge_api.task_queue import dispatch_sap_push_log

router = APIRouter(prefix="/sap-consultation-reviews", tags=["sap-consultation-reviews"])


class SapReviewBlockOut(BaseModel):
    recording_id: str
    file_name: str | None = None
    recording_created_at: str | None = None
    sap_summary_enabled: bool = True
    staff_id: str | None = None
    staff_name: str
    locked_header: str
    generated_body: str
    edited_body: str | None = None
    effective_body: str
    can_edit: bool = False
    sort_index: int = 0


class SapReviewRecordingFileOut(BaseModel):
    recording_id: str
    file_name: str | None = None
    created_at: str | None = None


class SapReviewListItemOut(BaseModel):
    visit_id: str
    review_id: str | None = None
    visit_order_no: str | None = None
    visit_order_seg: str | None = None
    customer_name: str | None = None
    customer_code: str | None = None
    hospital_code: str | None = None
    recording_count: int
    recording_file_names: list[str] = []
    recording_files: list[SapReviewRecordingFileOut] = []
    editable_block_count: int = 0
    status: str
    status_label: str
    latest_recording_at: str | None = None
    last_push_at: str | None = None
    last_success_push_at: str | None = None
    next_auto_push_at: str | None = None
    last_push_consultation_no: str | None = None
    last_push_error: str | None = None
    updated_at: str | None = None


class SapReviewDetailOut(SapReviewListItemOut):
    generated_text: str
    effective_text: str
    blocks: list[SapReviewBlockOut]
    indication_payload: list[dict[str, Any]] = []
    payload_snapshot: list[dict[str, Any]] = []
    latest_push_log: dict[str, Any] | None = None


class SapReviewBlockUpdateIn(BaseModel):
    editable_text: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _current_staff_id(user: User) -> str:
    staff_id = str(getattr(user, "staff_id", "") or "").strip()
    if not staff_id:
        raise HTTPException(403, "当前账号未绑定员工，无法查看 SAP 回写备注")
    return staff_id


def _status_filter_matches(status: str, status_filter: str) -> bool:
    normalized_status = str(status or "").strip()
    normalized_filter = str(status_filter or "").strip()
    if not normalized_filter or normalized_filter == "all":
        return True
    groups = {
        "pending": {"pending", "not_generated"},
        "sending": {"sending", "modified_sending"},
        "succeeded": {"succeeded", "modified_succeeded"},
        "failed": {"failed", "modified_failed"},
        "modified_pending": {"modified_pending"},
        "skipped": {"skipped"},
    }
    allowed = groups.get(normalized_filter)
    if allowed is not None:
        return normalized_status in allowed
    return normalized_status == normalized_filter


def _visit_order_key(visit: Visit) -> tuple[str, str | None] | None:
    visit_order_no = str(visit.external_visit_order_no or "").strip()
    if not visit_order_no:
        return None
    visit_order_seg = str(visit.external_visit_order_seg or "").strip() or None
    return visit_order_no, visit_order_seg


async def _load_visit_order_for_visit(db: AsyncSession, visit: Visit) -> VisitOrder | None:
    key = _visit_order_key(visit)
    if key is None:
        return None
    visit_order_no, visit_order_seg = key
    conditions = [VisitOrder.dzdh == visit_order_no]
    if visit_order_seg:
        conditions.append(VisitOrder.dzseg == visit_order_seg)
    else:
        conditions.append(or_(VisitOrder.dzseg.is_(None), VisitOrder.dzseg == ""))
    return (await db.execute(select(VisitOrder).where(*conditions).limit(1))).scalar_one_or_none()


async def _ensure_staff_has_visit_recording(db: AsyncSession, visit_id: str, staff_id: str) -> None:
    found = (
        await db.execute(
            select(Recording.id)
            .join(RecordingVisitLink, RecordingVisitLink.recording_id == Recording.id)
            .where(
                RecordingVisitLink.visit_id == visit_id,
                Recording.staff_id == staff_id,
                Recording.status != "filtered",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if not found:
        raise HTTPException(404, "SAP 回写记录不存在或当前账号无权查看")


def _review_has_user_edits(review: SapConsultationReview | None) -> bool:
    if review is None:
        return False
    return any(
        isinstance(block, dict) and str(block.get("edited_body") or "").strip()
        for block in (review.blocks if isinstance(review.blocks, list) else [])
    )


def _status_from_review_and_log(
    review: SapConsultationReview | None,
    latest_log: SapPushLog | None,
) -> tuple[str, str, str | None, datetime | None]:
    has_user_edits = _review_has_user_edits(review)
    if latest_log is None:
        if review is not None and (review.status == "modified" or has_user_edits):
            return "modified_pending", "已修改未回传", None, None
        return "pending", "待回传", None, None

    summary = summarize_sap_push_log_result(latest_log)
    effective_status = str(summary.get("effective_status") or latest_log.status or "").strip()
    last_activity = latest_log.sent_at or latest_log.updated_at or latest_log.created_at
    latest_log_matches_review = _review_effective_text_matches_push_log(review, latest_log)
    if has_user_edits and review is not None:
        if latest_log_matches_review:
            if effective_status in {"queued", "prepared", "sending"}:
                return "modified_sending", "已修改回传中", None, last_activity
            if effective_status == "succeeded":
                return "modified_succeeded", "已修改回传成功", None, last_activity
            if effective_status == "failed":
                return "modified_failed", "已修改回传失败", summary.get("effective_reason"), last_activity
        return "modified_pending", "已修改未回传", None, last_activity
    if effective_status in {"queued", "prepared", "sending"}:
        return "sending", "回传中", None, last_activity
    if review is not None and review.status == "pending" and not latest_log_matches_review:
        return "pending", "待回传", None, last_activity
    if effective_status == "succeeded":
        return "succeeded", "回传成功", None, last_activity
    if effective_status == "skipped":
        return "skipped", "未发送", summary.get("effective_reason"), last_activity
    if effective_status == "failed":
        return "failed", "回传失败", summary.get("effective_reason"), last_activity
    return effective_status or "pending", "待回传", summary.get("effective_reason"), last_activity


def _extract_consultation_no_from_text(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"咨询单(?:号)?[【\[\s：:]*([A-Za-z0-9]+)[】\]]?", text)
    if match:
        return match.group(1).strip() or None
    return None


def _extract_consultation_no_from_payload(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("ZXDH", "zxdh", "consultation_no", "consultationNo"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    msg = payload.get("msg")
    if isinstance(msg, str) and msg.strip():
        try:
            parsed = json.loads(msg)
        except ValueError:
            return _extract_consultation_no_from_text(msg)
        return _extract_consultation_no_from_payload(parsed) or _extract_consultation_no_from_text(msg)
    return _extract_consultation_no_from_text(payload.get("business_message") or payload.get("REMSG"))


def _extract_consultation_no_from_push_log(push_log: SapPushLog | None) -> str | None:
    if push_log is None:
        return None
    summary = summarize_sap_push_log_result(push_log)
    if str(summary.get("effective_status") or push_log.status or "").strip() != "succeeded":
        return None

    response_items = [item for item in (push_log.response_items or []) if isinstance(item, dict)]
    for item in reversed(response_items):
        if item.get("success") is False:
            continue
        consultation_no = (
            _extract_consultation_no_from_payload(item.get("response_body"))
            or _extract_consultation_no_from_text(item.get("business_message"))
            or _extract_consultation_no_from_text(item.get("retry_reason"))
        )
        if consultation_no:
            return consultation_no

    for payload in reversed(push_log.request_payloads or []):
        if not isinstance(payload, dict):
            continue
        zxxx = payload.get("zxxx")
        if isinstance(zxxx, dict):
            consultation_no = str(zxxx.get("zxdh") or zxxx.get("ZXDH") or "").strip()
            if consultation_no:
                return consultation_no
    return _extract_consultation_no_from_text(push_log.business_message)


def _extract_request_text_from_push_log(push_log: SapPushLog | None) -> str:
    if push_log is None:
        return ""
    for payload in push_log.request_payloads or []:
        if not isinstance(payload, dict):
            continue
        text = str(payload.get("text") or "").strip()
        if text:
            return text
    return ""


def _review_effective_text_matches_push_log(review: SapConsultationReview | None, push_log: SapPushLog | None) -> bool:
    review_text = str(getattr(review, "effective_text", "") or "").strip()
    push_text = _extract_request_text_from_push_log(push_log)
    return bool(review_text and push_text and review_text == push_text)


def _sanitize_editable_body(value: str, *, include_summary: bool = True) -> str:
    lines = []
    skipping_summary = False
    for raw_line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("●备注人员"):
            continue
        if stripped.startswith("●接诊人员"):
            continue
        if not include_summary:
            if re.match(r"^●\s*总结信息\s*[：:]", stripped):
                skipping_summary = True
                continue
            if skipping_summary and re.match(r"^●\s*[^：:\n]+?\s*[：:]", stripped):
                skipping_summary = False
            if skipping_summary:
                continue
        lines.append(raw_line.rstrip())
    return "\n".join(lines).strip()


def _split_consultation_block(text: str, staff_name: str) -> tuple[str, str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    locked_header = f"●备注人员：{staff_name or '无'}"
    if not normalized:
        return locked_header, ""
    lines = normalized.splitlines()
    first = lines[0].strip() if lines else ""
    if first.startswith("●备注人员"):
        return locked_header, _sanitize_editable_body("\n".join(lines[1:]))
    if first.startswith("●接诊人员"):
        return locked_header, _sanitize_editable_body("\n".join(lines[1:]))
    return locked_header, _sanitize_editable_body(normalized)


def _compose_review_text(blocks: list[dict[str, Any]], *, generated: bool = False) -> str:
    parts: list[str] = []
    for block in sorted(blocks, key=lambda item: int(item.get("sort_index") or 0)):
        header = str(block.get("locked_header") or "").strip()
        body = str(block.get("generated_body") if generated else block.get("effective_body") or "").strip()
        if not header and not body:
            continue
        parts.append("\n".join(part for part in (header, body) if part).strip())
    return "\n\n".join(parts).strip()


async def _load_latest_push_logs_for_visits(db: AsyncSession, visit_ids: list[str]) -> dict[str, SapPushLog]:
    if not visit_ids:
        return {}
    logs = (
        await db.execute(
            select(SapPushLog)
            .where(SapPushLog.visit_id.in_(visit_ids))
            .options(selectinload(SapPushLog.recording))
            .order_by(SapPushLog.created_at.desc())
        )
    ).scalars().all()
    by_visit: dict[str, SapPushLog] = {}
    for log in logs:
        if log.visit_id and log.visit_id not in by_visit:
            by_visit[log.visit_id] = log
    return by_visit


def _push_log_activity_at_value(log: SapPushLog | None) -> datetime | None:
    if log is None:
        return None
    return log.sent_at or log.updated_at or log.created_at


def _is_success_push_log(log: SapPushLog | None) -> bool:
    if log is None:
        return False
    summary = summarize_sap_push_log_result(log)
    return str(summary.get("effective_status") or log.status or "").strip() == "succeeded"


async def _load_latest_success_push_logs_for_visits(db: AsyncSession, visit_ids: list[str]) -> dict[str, SapPushLog]:
    if not visit_ids:
        return {}
    logs = (
        await db.execute(
            select(SapPushLog)
            .where(SapPushLog.visit_id.in_(visit_ids))
            .options(selectinload(SapPushLog.recording))
            .order_by(SapPushLog.created_at.desc())
        )
    ).scalars().all()
    by_visit: dict[str, SapPushLog] = {}
    for log in logs:
        visit_id = str(log.visit_id or "").strip()
        if not visit_id or visit_id in by_visit:
            continue
        if _is_success_push_log(log):
            by_visit[visit_id] = log
    return by_visit


async def _load_next_auto_push_times_for_visits(
    db: AsyncSession,
    visit_ids: list[str],
    *,
    reviews: dict[str, SapConsultationReview],
    latest_logs: dict[str, SapPushLog],
) -> dict[str, datetime]:
    if not visit_ids:
        return {}
    settings = get_settings()
    if not settings.sap_rfc_auto_push_on_bind:
        return {}

    stable_seconds = max(int(settings.sap_rfc_auto_push_stable_seconds or 0), 0)
    retry_delay_seconds = max(int(settings.sap_rfc_auto_push_retry_delay_seconds or 0), 0)
    recording_rows = (
        await db.execute(
            select(
                RecordingVisitLink.visit_id,
                Recording.id,
                Recording.status,
                Recording.created_at,
                RecordingVisitLink.created_at,
                RecordingVisitLink.updated_at,
            )
            .join(Recording, Recording.id == RecordingVisitLink.recording_id)
            .where(
                RecordingVisitLink.visit_id.in_(visit_ids),
                Recording.status != "filtered",
            )
        )
    ).all()

    recordings_by_visit: dict[str, list[tuple[str, str, datetime | None, datetime | None, datetime | None]]] = {}
    recording_ids: list[str] = []
    for visit_id, recording_id, status, recording_created_at, link_created_at, link_updated_at in recording_rows:
        normalized_visit_id = str(visit_id or "").strip()
        normalized_recording_id = str(recording_id or "").strip()
        if not normalized_visit_id or not normalized_recording_id:
            continue
        recordings_by_visit.setdefault(normalized_visit_id, []).append(
            (
                normalized_recording_id,
                str(status or "").strip(),
                recording_created_at,
                link_created_at,
                link_updated_at,
            )
        )
        recording_ids.append(normalized_recording_id)

    if not recording_ids:
        return {}

    analysis_file_names = [f"recording_{recording_id}.json" for recording_id in recording_ids]
    analysis_rows = (
        await db.execute(
            select(AnalysisTask.file_name, AnalysisTask.completed_at, AnalysisTask.updated_at)
            .where(
                AnalysisTask.file_name.in_(analysis_file_names),
                AnalysisTask.status == "done",
                AnalysisTask.result.is_not(None),
                cast(AnalysisTask.result, String) != "null",
            )
        )
    ).all()
    completed_analysis_by_file = {
        str(file_name or "").strip(): (completed_at or updated_at)
        for file_name, completed_at, updated_at in analysis_rows
        if str(file_name or "").strip()
    }

    result: dict[str, datetime] = {}
    for visit_id in visit_ids:
        normalized_visit_id = str(visit_id or "").strip()
        linked_recordings = recordings_by_visit.get(normalized_visit_id) or []
        if not linked_recordings:
            continue

        ready_at_values: list[datetime] = []
        all_ready = True
        for recording_id, status, recording_created_at, link_created_at, link_updated_at in linked_recordings:
            analysis_ready_at = completed_analysis_by_file.get(f"recording_{recording_id}.json")
            if status != "analyzed" or analysis_ready_at is None:
                all_ready = False
                break
            ready_at_values.append(analysis_ready_at)
            ready_at_values.append(link_updated_at or link_created_at or recording_created_at)
        if not all_ready:
            continue

        review = reviews.get(normalized_visit_id)
        if review is not None and review.updated_at is not None:
            ready_at_values.append(review.updated_at)
        has_user_edits = _review_has_user_edits(review)
        latest_log = latest_logs.get(normalized_visit_id)
        latest_log_status = str(
            summarize_sap_push_log_result(latest_log).get("effective_status") if latest_log is not None else ""
        ).strip() or str(getattr(latest_log, "status", "") or "").strip()
        if has_user_edits and (
            latest_log is None
            or not _review_effective_text_matches_push_log(review, latest_log)
            or latest_log_status not in {"failed", "skipped"}
        ):
            continue

        ready_at = max((value for value in ready_at_values if value is not None), default=None)
        if ready_at is None:
            continue

        next_auto_push_at = ready_at + timedelta(seconds=stable_seconds)
        if latest_log is not None and not _is_success_push_log(latest_log):
            if latest_log_status in {"failed", "skipped"}:
                activity_at = _push_log_activity_at_value(latest_log)
                if activity_at is not None:
                    next_auto_push_at = max(
                        next_auto_push_at,
                        activity_at + timedelta(seconds=retry_delay_seconds),
                    )
        result[normalized_visit_id] = next_auto_push_at
    return result


async def _load_push_log_for_serialization(db: AsyncSession, push_log_id: str | None) -> SapPushLog | None:
    normalized_id = str(push_log_id or "").strip()
    if not normalized_id:
        return None
    return (
        await db.execute(
            select(SapPushLog)
            .where(SapPushLog.id == normalized_id)
            .options(selectinload(SapPushLog.recording))
        )
    ).scalar_one_or_none()


async def _ensure_review(
    db: AsyncSession,
    visit_id: str,
    *,
    current_staff_id: str,
    preserve_status: bool = False,
) -> SapConsultationReview:
    await _ensure_staff_has_visit_recording(db, visit_id, current_staff_id)
    visit = await db.get(Visit, visit_id)
    if visit is None:
        raise HTTPException(404, "到诊单不存在")

    contexts, error = await _load_visit_recording_contexts(db, visit_id)
    if error:
        raise HTTPException(422, error.get("message") or "到诊单关联录音尚未完成分析")
    if not contexts:
        raise HTTPException(422, "该到诊单尚无可生成 SAP 回写的录音")

    source_recording = contexts[0].get("recording")
    if not isinstance(source_recording, Recording):
        raise HTTPException(422, "该到诊单尚无可生成 SAP 回写的录音")

    preview = await generate_sap_consultation_payloads(db, source_recording.id, target_visit_id=visit_id)
    if "error" in preview:
        raise HTTPException(422, preview.get("message") or "SAP 回写内容生成失败")

    visit_order = await _load_visit_order_for_visit(db, visit)
    hospital_code = str(getattr(visit_order, "jgbm", "") or "").strip()
    sap_summary_config = await _load_sap_summary_template_config(db, hospital_code) if hospital_code else None
    sap_summary_enabled = _is_sap_summary_section_enabled(visit_order, sap_summary_config)
    existing = (
        await db.execute(select(SapConsultationReview).where(SapConsultationReview.visit_id == visit_id))
    ).scalar_one_or_none()
    previous_status = str(getattr(existing, "status", "") or "").strip() if existing is not None else ""
    existing_blocks = {
        str(block.get("recording_id") or ""): block
        for block in (existing.blocks if existing is not None and isinstance(existing.blocks, list) else [])
        if isinstance(block, dict)
    }

    blocks: list[dict[str, Any]] = []
    for index, context in enumerate(contexts, start=1):
        recording = context.get("recording")
        if not isinstance(recording, Recording):
            continue
        staff = recording.__dict__.get("staff")
        staff_name = str(getattr(staff, "name", "") or "").strip() or "无"
        block_text = _build_recording_consultation_text_for_visit(
            context,
            sap_summary_config=sap_summary_config,
        )
        locked_header, generated_body = _split_consultation_block(block_text, staff_name)
        previous = existing_blocks.get(recording.id) or {}
        edited_body = _sanitize_editable_body(
            str(previous.get("edited_body") or ""),
            include_summary=sap_summary_enabled,
        )
        effective_body = edited_body or generated_body
        blocks.append(
            {
                "recording_id": recording.id,
                "file_name": recording.file_name,
                "recording_created_at": _iso(recording.created_at),
                "sap_summary_enabled": sap_summary_enabled,
                "staff_id": recording.staff_id,
                "staff_name": staff_name,
                "locked_header": locked_header,
                "generated_body": generated_body,
                "edited_body": edited_body or None,
                "effective_body": effective_body,
                "sort_index": index,
            }
        )

    generated_text = _compose_review_text(blocks, generated=True)
    effective_text = _compose_review_text(blocks, generated=False)
    payloads = list(preview.get("payloads") or [])
    if payloads and isinstance(payloads[0], dict):
        payloads[0] = {**payloads[0], "text": effective_text}
    indication_payload = list((payloads[0] or {}).get("TAB_SYZ") or []) if payloads else []

    now = _utcnow()
    is_new = existing is None
    if existing is None:
        existing = SapConsultationReview(
            visit_id=visit_id,
            created_by_staff_id=current_staff_id,
            created_at=now,
        )
        db.add(existing)

    next_values = {
        "visit_order_no": preview.get("visit_order_no") or getattr(visit_order, "dzdh", None),
        "visit_order_seg": preview.get("visit_order_seg") or getattr(visit_order, "dzseg", None),
        "hospital_code": getattr(visit_order, "jgbm", None),
        "customer_name": preview.get("customer_name") or getattr(visit_order, "ninam", None),
        "customer_code": preview.get("customer_code") or getattr(visit_order, "kunr", None),
        "recording_ids": [block["recording_id"] for block in blocks],
        "blocks": blocks,
        "generated_text": generated_text,
        "effective_text": effective_text,
        "indication_payload": indication_payload,
        "payload_snapshot": payloads,
    }
    changed = is_new or any(getattr(existing, key) != value for key, value in next_values.items())
    if changed:
        for key, value in next_values.items():
            setattr(existing, key, value)
        existing.updated_by_staff_id = current_staff_id
        existing.updated_at = now
        if any(block.get("edited_body") for block in blocks):
            existing.status = "modified"
        elif preserve_status and previous_status:
            existing.status = previous_status
        elif existing.status not in {"modified", "pushed"}:
            existing.status = "pending"

    await db.commit()
    await db.refresh(existing)
    return existing


def _review_blocks_out(review: SapConsultationReview, current_staff_id: str) -> list[SapReviewBlockOut]:
    blocks = review.blocks if isinstance(review.blocks, list) else []
    return [
        SapReviewBlockOut(
            recording_id=str(block.get("recording_id") or ""),
            file_name=block.get("file_name"),
            recording_created_at=str(block.get("recording_created_at") or block.get("created_at") or "").strip() or None,
            sap_summary_enabled=bool(block.get("sap_summary_enabled", True)),
            staff_id=block.get("staff_id"),
            staff_name=str(block.get("staff_name") or ""),
            locked_header=str(block.get("locked_header") or ""),
            generated_body=str(block.get("generated_body") or ""),
            edited_body=block.get("edited_body"),
            effective_body=str(block.get("effective_body") or ""),
            can_edit=str(block.get("staff_id") or "") == current_staff_id,
            sort_index=int(block.get("sort_index") or 0),
        )
        for block in blocks
        if isinstance(block, dict) and str(block.get("recording_id") or "")
    ]


def _enrich_sap_indication_payload(payload: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in payload or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        matched = resolve_indication_reference_item(
            department_code=str(row.get("CCKS") or row.get("department_code") or "").strip(),
            indication_code=str(row.get("CCSYZ") or row.get("indication_code") or "").strip(),
            body_part_code=str(row.get("CCBW") or row.get("body_part_code") or "").strip(),
        )
        if matched is not None:
            row.update(
                {
                    "department_code": matched.department_code,
                    "department_name": matched.department_name,
                    "indication_code": matched.indication_code,
                    "indication_name": matched.indication_name,
                    "body_part_code": matched.body_part_code,
                    "body_part_name": matched.body_part_name,
                }
            )
        else:
            row.setdefault("department_code", str(row.get("CCKS") or "").strip())
            row.setdefault("indication_code", str(row.get("CCSYZ") or "").strip())
            row.setdefault("body_part_code", str(row.get("CCBW") or "").strip())
        rows.append(row)
    return rows


def _detail_out(
    review: SapConsultationReview,
    *,
    current_staff_id: str,
    latest_log: SapPushLog | None,
    recording_count: int | None = None,
) -> SapReviewDetailOut:
    status, status_label, error, last_push_at = _status_from_review_and_log(review, latest_log)
    blocks = _review_blocks_out(review, current_staff_id)
    recording_file_names = [block.file_name for block in blocks if block.file_name]
    recording_files = [
        SapReviewRecordingFileOut(
            recording_id=block.recording_id,
            file_name=block.file_name,
            created_at=block.recording_created_at,
        )
        for block in blocks
    ]
    return SapReviewDetailOut(
        visit_id=review.visit_id,
        review_id=review.id,
        visit_order_no=review.visit_order_no,
        visit_order_seg=review.visit_order_seg,
        customer_name=review.customer_name,
        customer_code=review.customer_code,
        hospital_code=review.hospital_code,
        recording_count=recording_count or len(blocks),
        recording_file_names=recording_file_names,
        recording_files=recording_files,
        editable_block_count=sum(1 for block in blocks if block.can_edit),
        status=status,
        status_label=status_label,
        latest_recording_at=None,
        last_push_at=_iso(last_push_at),
        last_push_consultation_no=_extract_consultation_no_from_push_log(latest_log),
        last_push_error=error,
        updated_at=_iso(review.updated_at),
        generated_text=review.generated_text or "",
        effective_text=review.effective_text or "",
        blocks=blocks,
        indication_payload=_enrich_sap_indication_payload(list(review.indication_payload or [])),
        payload_snapshot=list(review.payload_snapshot or []),
        latest_push_log=serialize_sap_push_log(latest_log) if latest_log is not None else None,
    )


@router.get("", response_model=PaginatedResponse[SapReviewListItemOut])
async def list_sap_consultation_reviews(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str | None = Query(None),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    staff_id = _current_staff_id(current_user)
    normalized_keyword = str(keyword or "").strip()
    normalized_status_filter = str(status or "").strip()
    has_status_filter = bool(normalized_status_filter and normalized_status_filter != "all")

    stmt = (
        select(
            Visit.id.label("visit_id"),
            Visit.external_visit_order_no.label("visit_order_no"),
            Visit.external_visit_order_seg.label("visit_order_seg"),
            func.count(distinct(Recording.id)).label("recording_count"),
            func.max(Recording.created_at).label("latest_recording_at"),
        )
        .join(RecordingVisitLink, RecordingVisitLink.visit_id == Visit.id)
        .join(Recording, Recording.id == RecordingVisitLink.recording_id)
        .where(Recording.staff_id == staff_id, Recording.status != "filtered")
        .group_by(Visit.id, Visit.external_visit_order_no, Visit.external_visit_order_seg)
        .order_by(func.max(Recording.created_at).desc(), Visit.id.desc())
    )
    if normalized_keyword:
        like_value = f"%{normalized_keyword}%"
        stmt = stmt.where(
            or_(
                Visit.external_visit_order_no.ilike(like_value),
                Visit.external_visit_order_seg.ilike(like_value),
                Recording.file_name.ilike(like_value),
            )
        )

    if has_status_filter:
        rows = (await db.execute(stmt)).all()
        total = 0
    else:
        total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        rows = (await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))).all()
    visit_ids = [str(row.visit_id) for row in rows]
    latest_logs = await _load_latest_push_logs_for_visits(db, visit_ids)
    latest_success_logs = await _load_latest_success_push_logs_for_visits(db, visit_ids)
    recording_files_by_visit: dict[str, list[SapReviewRecordingFileOut]] = {}
    if visit_ids:
        recording_name_rows = (
            await db.execute(
                select(RecordingVisitLink.visit_id, Recording.id, Recording.file_name, Recording.created_at)
                .join(Recording, Recording.id == RecordingVisitLink.recording_id)
                .where(
                    RecordingVisitLink.visit_id.in_(visit_ids),
                    Recording.staff_id == staff_id,
                    Recording.status != "filtered",
                )
                .order_by(RecordingVisitLink.visit_id.asc(), Recording.created_at.asc(), Recording.id.asc())
            )
        ).all()
        for row_visit_id, recording_id, file_name, created_at in recording_name_rows:
            normalized_name = str(file_name or "").strip()
            files = recording_files_by_visit.setdefault(str(row_visit_id), [])
            normalized_recording_id = str(recording_id or "").strip()
            if any(item.recording_id == normalized_recording_id for item in files):
                continue
            files.append(
                SapReviewRecordingFileOut(
                    recording_id=normalized_recording_id,
                    file_name=normalized_name or None,
                    created_at=_iso(created_at),
                )
            )
    reviews = {
        review.visit_id: review
        for review in (
            await db.execute(select(SapConsultationReview).where(SapConsultationReview.visit_id.in_(visit_ids)))
        ).scalars().all()
    } if visit_ids else {}
    next_auto_push_times = await _load_next_auto_push_times_for_visits(
        db,
        visit_ids,
        reviews=reviews,
        latest_logs=latest_logs,
    )

    order_keys = {
        (str(row.visit_order_no or ""), str(row.visit_order_seg or "").strip() or None)
        for row in rows
        if str(row.visit_order_no or "").strip()
    }
    order_filters = []
    for visit_order_no, visit_order_seg in order_keys:
        if visit_order_seg:
            order_filters.append((VisitOrder.dzdh == visit_order_no) & (VisitOrder.dzseg == visit_order_seg))
        else:
            order_filters.append((VisitOrder.dzdh == visit_order_no) & (or_(VisitOrder.dzseg.is_(None), VisitOrder.dzseg == "")))
    visit_orders = (
        await db.execute(select(VisitOrder).where(or_(*order_filters)))
    ).scalars().all() if order_filters else []
    order_by_key = {
        (str(order.dzdh or ""), str(order.dzseg or "").strip() or None): order
        for order in visit_orders
    }

    items: list[SapReviewListItemOut] = []
    for row in rows:
        visit_id = str(row.visit_id)
        review = reviews.get(visit_id)
        latest_log = latest_logs.get(visit_id)
        latest_success_log = latest_success_logs.get(visit_id)
        status, status_label, error, last_push_at = _status_from_review_and_log(review, latest_log)
        order = order_by_key.get((str(row.visit_order_no or ""), str(row.visit_order_seg or "").strip() or None))
        blocks = review.blocks if review is not None and isinstance(review.blocks, list) else []
        editable_count = sum(1 for block in blocks if isinstance(block, dict) and str(block.get("staff_id") or "") == staff_id)
        recording_files = recording_files_by_visit.get(visit_id, [])
        item = SapReviewListItemOut(
            visit_id=visit_id,
            review_id=review.id if review else None,
            visit_order_no=str(row.visit_order_no or "") or getattr(order, "dzdh", None),
            visit_order_seg=str(row.visit_order_seg or "") or getattr(order, "dzseg", None),
            customer_name=(review.customer_name if review else None) or getattr(order, "ninam", None),
            customer_code=(review.customer_code if review else None) or getattr(order, "kunr", None),
            hospital_code=(review.hospital_code if review else None) or getattr(order, "jgbm", None),
            recording_count=int(row.recording_count or 0),
            recording_file_names=[item.file_name for item in recording_files if item.file_name],
            recording_files=recording_files,
            editable_block_count=editable_count,
            status=status,
            status_label=status_label,
            latest_recording_at=_iso(row.latest_recording_at),
            last_push_at=_iso(last_push_at),
            last_success_push_at=_iso(_push_log_activity_at_value(latest_success_log)),
            next_auto_push_at=_iso(next_auto_push_times.get(visit_id)),
            last_push_consultation_no=_extract_consultation_no_from_push_log(latest_success_log),
            last_push_error=error,
            updated_at=_iso(review.updated_at) if review else None,
        )
        if has_status_filter and not _status_filter_matches(item.status, normalized_status_filter):
            continue
        items.append(item)

    if has_status_filter:
        total = len(items)
        start = (page - 1) * page_size
        items = items[start:start + page_size]

    return make_page_response(items, int(total or 0), page, page_size)


@router.get("/visits/{visit_id}", response_model=SapReviewDetailOut)
async def get_sap_consultation_review(
    visit_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    staff_id = _current_staff_id(current_user)
    review = await _ensure_review(db, visit_id, current_staff_id=staff_id, preserve_status=True)
    latest_log = (await _load_latest_push_logs_for_visits(db, [visit_id])).get(visit_id)
    return _detail_out(review, current_staff_id=staff_id, latest_log=latest_log)


@router.patch("/visits/{visit_id}/blocks/{recording_id}", response_model=SapReviewDetailOut)
async def update_sap_consultation_review_block(
    visit_id: str,
    recording_id: str,
    body: SapReviewBlockUpdateIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    staff_id = _current_staff_id(current_user)
    review = await _ensure_review(db, visit_id, current_staff_id=staff_id)
    blocks = list(review.blocks or [])
    updated = False
    for block in blocks:
        if not isinstance(block, dict) or str(block.get("recording_id") or "") != recording_id:
            continue
        if str(block.get("staff_id") or "") != staff_id:
            raise HTTPException(403, "只能修改自己录音对应的咨询备注部分")
        sanitized_body = _sanitize_editable_body(
            body.editable_text,
            include_summary=bool(block.get("sap_summary_enabled", True)),
        )
        generated_body = str(block.get("generated_body") or "").strip()
        current_edited_body = _sanitize_editable_body(
            str(block.get("edited_body") or ""),
            include_summary=bool(block.get("sap_summary_enabled", True)),
        )
        next_edited_body = sanitized_body if sanitized_body and sanitized_body != generated_body else None
        next_effective_body = next_edited_body or generated_body
        current_effective_body = str(block.get("effective_body") or "").strip()
        if next_effective_body == current_effective_body and (next_edited_body or "") == current_edited_body:
            raise HTTPException(400, "内容未修改，无需保存")
        block["edited_body"] = next_edited_body
        block["effective_body"] = next_effective_body
        updated = True
        break
    if not updated:
        raise HTTPException(404, "未找到可编辑的录音备注块")

    review.blocks = blocks
    review.effective_text = _compose_review_text(blocks, generated=False)
    latest_log = (await _load_latest_push_logs_for_visits(db, [visit_id])).get(visit_id)
    if any(isinstance(block, dict) and str(block.get("edited_body") or "").strip() for block in blocks):
        review.status = "modified"
    elif _review_effective_text_matches_push_log(review, latest_log) and str(
        summarize_sap_push_log_result(latest_log).get("effective_status") if latest_log else ""
    ) == "succeeded":
        review.status = latest_log.status or "succeeded"
        review.last_push_log_id = latest_log.id
    else:
        review.status = "pending"
    review.updated_by_staff_id = staff_id
    review.updated_at = _utcnow()
    await db.commit()
    await db.refresh(review)
    return _detail_out(review, current_staff_id=staff_id, latest_log=latest_log)


@router.post("/visits/{visit_id}/push")
async def push_sap_consultation_review(
    visit_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    staff_id = _current_staff_id(current_user)
    review = await _ensure_review(db, visit_id, current_staff_id=staff_id)
    latest_log = (await _load_latest_push_logs_for_visits(db, [visit_id])).get(visit_id)
    status, _status_label, _error, _last_push_at = _status_from_review_and_log(review, latest_log)
    if status not in {"modified_pending", "modified_failed"}:
        raise HTTPException(422, "咨询备注未修改，无需手动提交回传")
    recording_id = next((str(block.get("recording_id") or "") for block in review.blocks or [] if isinstance(block, dict)), "")
    if not recording_id:
        raise HTTPException(422, "缺少可用于回传的录音")
    try:
        push_log = await create_sap_push_log(
            db,
            recording_id,
            target_visit_id=visit_id,
            trigger_mode="manual",
            initiated_by=current_user.display_name or current_user.username,
            prefer_async=True,
        )
    except SapPushPreparationError as exc:
        raise HTTPException(422, exc.message) from exc

    settings = get_settings()
    queued = False
    message = "已创建 SAP 回写任务"
    if push_log.status == "queued":
        await dispatch_sap_push_log(push_log.id)
        queued = True
        message = "已提交 SAP 回写队列"
    elif push_log.status == "prepared":
        executed = await execute_sap_push_log(push_log.id)
        if executed is not None:
            push_log = executed
        message = "已执行 SAP 回写"
    elif push_log.status == "skipped":
        message = "SAP 回写已关闭，已保存日志但未发送"

    review.last_push_log_id = push_log.id
    review.status = "sending" if queued else push_log.status
    review.updated_by_staff_id = staff_id
    review.updated_at = _utcnow()
    await db.commit()
    await db.refresh(review)
    serializable_log = await _load_push_log_for_serialization(db, push_log.id)
    return {
        "queued": queued,
        "dispatch_mode": settings.sap_rfc_dispatch_mode,
        "send_enabled": bool(settings.sap_rfc_send_enabled),
        "message": message,
        "log": serialize_sap_push_log(serializable_log or push_log),
    }
