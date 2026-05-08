from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import (
    AnalysisTask,
    Recording,
    RecordingCustomerSegment,
    RecordingVisitAnalysis,
    RecordingVisitLink,
    Transcript,
    User,
    Visit,
)
from smart_badge_api.recording_analysis_service import (
    build_analysis_payload_from_utterances,
    ensure_analysis_input_dir,
)
from smart_badge_api.task_queue import dispatch_analysis_task
from smart_badge_api.visit_linking import ordered_recording_visit_links


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _is_customer_like_utterance(utterance: dict[str, Any]) -> bool:
    role_text = " ".join(
        _clean_text(utterance.get(key)).lower()
        for key in ("speaker_business_role", "speaker_role", "speaker", "role")
    )
    return any(token in role_text for token in ("customer", "client", "primary_customer", "客户", "主客户", "同行人"))


def _is_staff_like_utterance(utterance: dict[str, Any]) -> bool:
    role_text = " ".join(
        _clean_text(utterance.get(key)).lower()
        for key in ("speaker_business_role", "speaker_role", "speaker", "role", "speaker_display_label")
    )
    return any(
        token in role_text
        for token in (
            "consultant",
            "advisor",
            "doctor",
            "staff",
            "employee",
            "system_context",
            "咨询",
            "顾问",
            "医生",
            "员工",
            "工牌",
            "设计师",
        )
    )


def _speaker_key(utterance: dict[str, Any]) -> str:
    for key in ("speaker_id", "speaker", "speaker_label", "speaker_display_label"):
        value = _clean_text(utterance.get(key))
        if value:
            return value.lower()
    return ""


def _summarize_utterances(utterances: list[dict[str, Any]]) -> str:
    preferred = [item for item in utterances if _is_customer_like_utterance(item)]
    source = preferred or utterances
    snippets: list[str] = []
    for item in source:
        text = _clean_text(item.get("text"))
        if not text:
            continue
        snippets.append(text[:80])
        if len(snippets) >= 3:
            break
    return "；".join(snippets) if snippets else "暂无可用原文，请查看完整转写辅助判断"


def _split_evenly(utterances: list[dict[str, Any]], segment_count: int) -> list[list[int]]:
    chunk_size = max(1, math.ceil(len(utterances) / segment_count))
    chunks = [
        list(range(index, min(index + chunk_size, len(utterances))))
        for index in range(0, len(utterances), chunk_size)
    ]
    while len(chunks) < segment_count:
        chunks.append([])
    if len(chunks) > segment_count:
        overflow = [item for chunk in chunks[segment_count - 1:] for item in chunk]
        chunks = chunks[: segment_count - 1] + [overflow]
    return chunks


def _split_by_customer_speakers(utterances: list[dict[str, Any]], segment_count: int) -> list[list[int]] | None:
    speaker_indexes: dict[str, list[int]] = {}
    speaker_text_len: dict[str, int] = {}
    for index, utterance in enumerate(utterances):
        if not _is_customer_like_utterance(utterance) or _is_staff_like_utterance(utterance):
            continue
        key = _speaker_key(utterance)
        if not key:
            continue
        speaker_indexes.setdefault(key, []).append(index)
        speaker_text_len[key] = speaker_text_len.get(key, 0) + len(_clean_text(utterance.get("text")))

    candidates = [
        (key, indexes)
        for key, indexes in speaker_indexes.items()
        if len(indexes) >= 2 or speaker_text_len.get(key, 0) >= 20
    ]
    if len(candidates) < segment_count:
        return None

    # Keep the conversation order. In real companion consultations the first
    # customer usually appears before the second, and this is easier to verify
    # manually than sorting by volume.
    candidates.sort(key=lambda item: item[1][0])
    selected = candidates[:segment_count]
    selected_keys = {key for key, _indexes in selected}
    groups: list[list[int]] = []
    for key, indexes in selected:
        expanded: set[int] = set(indexes)
        for index in indexes:
            for neighbor_index in (index - 1, index + 1):
                if neighbor_index < 0 or neighbor_index >= len(utterances):
                    continue
                neighbor = utterances[neighbor_index]
                neighbor_key = _speaker_key(neighbor)
                if neighbor_key == key or not (_is_customer_like_utterance(neighbor) and neighbor_key in selected_keys):
                    expanded.add(neighbor_index)
        groups.append(sorted(expanded))

    return groups if all(groups) else None


def _split_by_time_gaps(utterances: list[dict[str, Any]], segment_count: int) -> list[list[int]] | None:
    if len(utterances) < segment_count * 2:
        return None

    gaps: list[tuple[int, int]] = []
    for index in range(1, len(utterances)):
        prev_end = int(utterances[index - 1].get("end_ms") or 0)
        current_begin = int(utterances[index].get("begin_ms") or 0)
        gap_ms = max(0, current_begin - prev_end)
        if gap_ms >= 1500:
            gaps.append((gap_ms, index))
    if len(gaps) < segment_count - 1:
        return None

    cut_points = sorted(index for _gap_ms, index in sorted(gaps, reverse=True)[: segment_count - 1])
    chunks: list[list[int]] = []
    start = 0
    for cut_point in cut_points:
        chunks.append(list(range(start, cut_point)))
        start = cut_point
    chunks.append(list(range(start, len(utterances))))
    return chunks if len(chunks) == segment_count and all(chunks) else None


def _split_utterance_indexes(utterances: list[dict[str, Any]], segment_count: int) -> list[list[int]]:
    if not utterances or segment_count <= 0:
        return [[] for _ in range(max(segment_count, 0))]

    if segment_count == 1:
        return [list(range(len(utterances)))]

    speaker_groups = _split_by_customer_speakers(utterances, segment_count)
    if speaker_groups is not None:
        return speaker_groups

    time_gap_groups = _split_by_time_gaps(utterances, segment_count)
    if time_gap_groups is not None:
        return time_gap_groups

    return _split_evenly(utterances, segment_count)


def _coerce_utterances(transcript: Transcript | None) -> list[dict[str, Any]]:
    utterances = transcript.utterances if transcript else []
    if not isinstance(utterances, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in utterances:
        if not isinstance(item, dict):
            continue
        text = _clean_text(item.get("text"))
        if not text:
            continue
        copied = dict(item)
        copied["text"] = text
        copied["begin_ms"] = int(copied.get("begin_ms") or copied.get("begin") or 0)
        copied["end_ms"] = int(copied.get("end_ms") or copied.get("end") or copied["begin_ms"])
        normalized.append(copied)
    normalized.sort(key=lambda item: (int(item.get("begin_ms") or 0), int(item.get("end_ms") or 0)))
    return normalized


async def _load_recording_with_review_context(db: AsyncSession, recording_id: str) -> Recording | None:
    return (
        await db.execute(
            select(Recording)
            .where(Recording.id == recording_id)
            .options(
                selectinload(Recording.transcript),
                selectinload(Recording.staff),
                selectinload(Recording.visit_links)
                .selectinload(RecordingVisitLink.visit)
                .selectinload(Visit.customer),
                selectinload(Recording.customer_segments),
                selectinload(Recording.visit_analyses).selectinload(RecordingVisitAnalysis.analysis_task),
                selectinload(Recording.visit_analyses).selectinload(RecordingVisitAnalysis.visit).selectinload(Visit.customer),
                selectinload(Recording.visit_analyses).selectinload(RecordingVisitAnalysis.customer_segment),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def sync_visit_analysis_task_result(db: AsyncSession, analysis: RecordingVisitAnalysis) -> None:
    if not analysis.analysis_task_id:
        return
    task = analysis.analysis_task or await db.get(AnalysisTask, analysis.analysis_task_id)
    if task is None:
        analysis.analysis_status = "failed"
        analysis.analysis_error = "关联的分析任务不存在"
        return

    if task.status == "done" and task.result:
        analysis.analysis_status = "done"
        analysis.analysis_result = dict(task.result)
        analysis.analysis_error = None
        analysis.sap_ready_at = task.completed_at or _utcnow()
    elif task.status in {"pending", "running"}:
        analysis.analysis_status = task.status
        analysis.analysis_error = None
    elif task.status == "failed":
        analysis.analysis_status = "failed"
        analysis.analysis_error = task.error_message or "分析任务失败"


async def sync_visit_analysis_results(db: AsyncSession, recording_id: str | None = None) -> None:
    stmt = select(RecordingVisitAnalysis).options(selectinload(RecordingVisitAnalysis.analysis_task))
    if recording_id:
        stmt = stmt.where(RecordingVisitAnalysis.recording_id == recording_id)
    rows = (await db.execute(stmt)).scalars().all()
    for row in rows:
        await sync_visit_analysis_task_result(db, row)


async def ensure_multi_customer_review(db: AsyncSession, recording_id: str) -> Recording | None:
    recording = await _load_recording_with_review_context(db, recording_id)
    if recording is None:
        return None

    links = [link for link in ordered_recording_visit_links(recording) if link.visit is not None]
    if len(links) <= 1:
        return recording

    utterances = _coerce_utterances(recording.transcript)
    chunks = _split_utterance_indexes(utterances, len(links))
    existing_segments = {segment.segment_index: segment for segment in recording.customer_segments}
    for index, utterance_indexes in enumerate(chunks, start=1):
        chunk_utterances = [utterances[item] for item in utterance_indexes if 0 <= item < len(utterances)]
        begin_ms = int(chunk_utterances[0].get("begin_ms") or 0) if chunk_utterances else 0
        end_ms = int(chunk_utterances[-1].get("end_ms") or begin_ms) if chunk_utterances else begin_ms
        segment = existing_segments.get(index)
        if segment is None:
            segment = RecordingCustomerSegment(
                recording_id=recording_id,
                segment_index=index,
                label=f"客户{index}",
            )
            db.add(segment)
        segment.begin_ms = begin_ms
        segment.end_ms = max(end_ms, begin_ms)
        segment.summary = _summarize_utterances(chunk_utterances)
        segment.utterance_indexes = utterance_indexes
        segment.utterance_count = len(chunk_utterances)
        segment.status = "detected"

    current_visit_ids = {link.visit_id for link in links}
    existing_analyses = {analysis.visit_id: analysis for analysis in recording.visit_analyses}
    for visit_id, analysis in list(existing_analyses.items()):
        if visit_id not in current_visit_ids:
            await db.delete(analysis)

    for link in links:
        if link.visit_id not in existing_analyses:
            db.add(
                RecordingVisitAnalysis(
                    recording_id=recording_id,
                    visit_id=link.visit_id,
                    mapping_status="pending",
                    analysis_status="idle",
                )
            )

    await db.flush()
    await sync_visit_analysis_results(db, recording_id)
    await db.flush()
    return await _load_recording_with_review_context(db, recording_id)


def _build_visit_analysis_context_utterance(recording: Recording, visit: Visit | None, segment: RecordingCustomerSegment) -> dict[str, Any]:
    visit_ref = ""
    customer_name = ""
    customer_code = ""
    if visit is not None:
        visit_ref = "-".join(
            part
            for part in [
                _clean_text(visit.external_visit_order_no),
                _clean_text(visit.external_visit_order_seg),
            ]
            if part
        )
        customer_name = _clean_text(visit.customer.name if visit.customer else "")
        customer_code = _clean_text(visit.customer.external_customer_code if visit.customer else "")
    target_label = "，".join(
        item
        for item in [
            f"客户段={segment.label}",
            f"到诊单={visit_ref}" if visit_ref else "",
            f"客户姓名={customer_name}" if customer_name else "",
            f"客户编码={customer_code}" if customer_code else "",
        ]
        if item
    )
    return {
        "speaker_role": "system_context",
        "speaker_business_role": "system_context",
        "speaker_display_label": "分析目标",
        "speaker_id": "analysis_target",
        "begin_ms": segment.begin_ms,
        "end_ms": segment.begin_ms,
        "text": (
            f"【分析目标】本次只分析{target_label or '当前确认客户'}。"
            "如果系统全局规则提到“主客户/主咨询线”，本任务中应以这里指定的客户段和到诊单作为主客户；"
            "请只提取该客户本人的主诉、适应症、画像标签、预算、顾虑、推荐方案、成交状态和SAP回传内容；"
            "不要混入本录音中其他客户的诉求或治疗历史。"
        ),
    }


async def create_or_dispatch_visit_scoped_analysis(
    db: AsyncSession,
    analysis: RecordingVisitAnalysis,
) -> AnalysisTask:
    recording = await _load_recording_with_review_context(db, analysis.recording_id)
    if recording is None:
        raise ValueError("Recording not found")
    if recording.transcript is None or recording.transcript.status != "completed":
        raise ValueError("Transcript is not ready")
    if analysis.customer_segment_id is None:
        raise ValueError("Customer segment mapping is not confirmed")

    segment = await db.get(RecordingCustomerSegment, analysis.customer_segment_id)
    if segment is None:
        raise ValueError("Customer segment not found")
    visit = await db.get(Visit, analysis.visit_id, options=[selectinload(Visit.customer)])

    all_utterances = _coerce_utterances(recording.transcript)
    indexes = [int(item) for item in (segment.utterance_indexes or []) if isinstance(item, int) or str(item).isdigit()]
    selected_utterances = [all_utterances[index] for index in indexes if 0 <= index < len(all_utterances)]
    if not selected_utterances:
        selected_utterances = [
            item
            for item in all_utterances
            if int(item.get("begin_ms") or 0) >= segment.begin_ms and int(item.get("end_ms") or 0) <= segment.end_ms
        ]
    if not selected_utterances:
        raise ValueError("Customer segment has no valid utterances")

    analysis_utterances = [
        _build_visit_analysis_context_utterance(recording, visit, segment),
        *selected_utterances,
    ]
    payload, segment_count, duration_ms = build_analysis_payload_from_utterances(
        analysis_utterances,
        staff_id=recording.staff_id,
        staff_name=recording.staff.name if recording.staff else None,
        staff_role=recording.staff.role if recording.staff else None,
    )
    if segment_count == 0:
        raise ValueError("Transcript has no valid utterances")

    analysis_file_name = f"recording_{analysis.recording_id}_visit_{analysis.visit_id}.json"
    existing = (
        await db.execute(
            select(AnalysisTask)
            .where(
                AnalysisTask.file_name == analysis_file_name,
                AnalysisTask.status.in_(["pending", "running"]),
            )
            .order_by(AnalysisTask.created_at.desc())
        )
    ).scalars().first()
    if existing:
        analysis.analysis_task_id = existing.id
        analysis.analysis_status = existing.status
        await db.commit()
        return existing

    input_path = ensure_analysis_input_dir() / analysis_file_name
    input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    task = AnalysisTask(
        file_name=analysis_file_name,
        file_path=get_settings().make_relative_path(input_path.resolve()),
        segment_count=segment_count,
        duration_ms=duration_ms,
    )
    db.add(task)
    await db.flush()
    analysis.analysis_task_id = task.id
    analysis.analysis_status = "pending"
    analysis.analysis_result = None
    analysis.analysis_error = None
    analysis.sap_ready_at = None
    await db.commit()
    await db.refresh(task)

    try:
        ran_inline = await dispatch_analysis_task(task.id)
    except Exception:
        task.status = "failed"
        task.error_message = "Failed to dispatch analysis task"
        analysis.analysis_status = "failed"
        analysis.analysis_error = task.error_message
        await db.commit()
        raise

    if ran_inline:
        await db.refresh(task)
        await db.refresh(analysis)
        await sync_visit_analysis_task_result(db, analysis)
        await db.commit()
    return task


async def confirm_multi_customer_mappings(
    db: AsyncSession,
    recording_id: str,
    mappings: list[dict[str, str]],
    *,
    confirmed_by_user: User | None = None,
) -> Recording:
    recording = await ensure_multi_customer_review(db, recording_id)
    if recording is None:
        raise ValueError("Recording not found")

    links = [link for link in ordered_recording_visit_links(recording) if link.visit is not None]
    if len(links) <= 1:
        raise ValueError("当前录音没有关联多个到诊单")

    linked_visit_ids = {link.visit_id for link in links}
    segment_ids = {segment.id for segment in recording.customer_segments}
    requested_visit_ids = {_clean_text(item.get("visit_id")) for item in mappings}
    requested_segment_ids = [_clean_text(item.get("customer_segment_id")) for item in mappings]
    if requested_visit_ids != linked_visit_ids:
        raise ValueError("需要为当前录音关联的每一张到诊单选择客户段")
    if any(segment_id not in segment_ids for segment_id in requested_segment_ids):
        raise ValueError("客户段不属于当前录音")
    if len(set(requested_segment_ids)) != len(requested_segment_ids):
        raise ValueError("每个客户段只能对应一张到诊单")

    analyses = {analysis.visit_id: analysis for analysis in recording.visit_analyses}
    now = _utcnow()
    confirmed_by = (
        confirmed_by_user.display_name
        or confirmed_by_user.username
        if confirmed_by_user is not None
        else None
    )
    analyses_to_dispatch: list[RecordingVisitAnalysis] = []
    for item in mappings:
        visit_id = _clean_text(item.get("visit_id"))
        segment_id = _clean_text(item.get("customer_segment_id"))
        analysis = analyses.get(visit_id)
        if analysis is None:
            analysis = RecordingVisitAnalysis(recording_id=recording_id, visit_id=visit_id)
            db.add(analysis)
        mapping_changed = analysis.customer_segment_id != segment_id or analysis.mapping_status != "confirmed"
        analysis.customer_segment_id = segment_id
        analysis.mapping_status = "confirmed"
        analysis.confirmed_by = confirmed_by
        analysis.confirmed_at = now
        if mapping_changed or analysis.analysis_status not in {"pending", "running", "done"}:
            analysis.analysis_status = "pending"
            analysis.analysis_task_id = None
            analysis.analysis_result = None
            analysis.analysis_error = None
            analysis.sap_ready_at = None
            analysis.sap_push_log_id = None
            analyses_to_dispatch.append(analysis)

    await db.commit()
    for analysis in analyses_to_dispatch:
        await db.refresh(analysis)
        try:
            await create_or_dispatch_visit_scoped_analysis(db, analysis)
        except Exception as exc:
            analysis.analysis_status = "failed"
            analysis.analysis_error = str(exc) or "到诊单级分析任务创建失败"
            analysis.analysis_task_id = None
            analysis.analysis_result = None
            analysis.sap_ready_at = None
            await db.commit()

    refreshed = await ensure_multi_customer_review(db, recording_id)
    if refreshed is None:
        raise ValueError("Recording not found")
    await db.commit()
    return refreshed


async def reset_multi_customer_mappings(
    db: AsyncSession,
    recording_id: str,
    *,
    reset_by_user: User | None = None,
) -> Recording:
    recording = await ensure_multi_customer_review(db, recording_id)
    if recording is None:
        raise ValueError("Recording not found")

    links = [link for link in ordered_recording_visit_links(recording) if link.visit is not None]
    if len(links) <= 1:
        raise ValueError("当前录音没有关联多个到诊单")

    reset_by = (
        reset_by_user.display_name
        or reset_by_user.username
        if reset_by_user is not None
        else None
    )
    reset_note = f"已解除客户对应确认{f'（操作人：{reset_by}）' if reset_by else ''}"
    for analysis in recording.visit_analyses:
        analysis.customer_segment_id = None
        analysis.mapping_status = "pending"
        analysis.analysis_status = "idle"
        analysis.analysis_task_id = None
        analysis.analysis_result = None
        analysis.analysis_error = reset_note
        analysis.confirmed_by = None
        analysis.confirmed_at = None
        analysis.sap_ready_at = None
        analysis.sap_push_log_id = None

    await db.commit()
    refreshed = await ensure_multi_customer_review(db, recording_id)
    if refreshed is None:
        raise ValueError("Recording not found")
    await db.commit()
    return refreshed


def is_recording_multi_customer_ready(recording: Recording) -> bool:
    links = [link for link in ordered_recording_visit_links(recording) if link.visit is not None]
    if len(links) <= 1:
        return True
    analyses_by_visit_id = {analysis.visit_id: analysis for analysis in recording.visit_analyses}
    for link in links:
        analysis = analyses_by_visit_id.get(link.visit_id)
        if analysis is None:
            return False
        if analysis.mapping_status != "confirmed" or analysis.analysis_status != "done" or not analysis.analysis_result:
            return False
    return True
