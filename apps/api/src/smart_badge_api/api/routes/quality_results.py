from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.api.analysis_access import (
    ensure_task_visible,
)
from smart_badge_api.api.analysis_normalization import normalize_analysis_result
from smart_badge_api.api.data_scope import build_permission_scope, recording_scope_condition
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.db.models import AnalysisTask, Customer, Recording, Staff, Visit
from smart_badge_api.db.models import User
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.pagination import PaginatedResponse, make_page_response
from smart_badge_api.schemas.quality_results import (
    QualityResultDetailOut,
    QualityResultDimensionOut,
    QualityResultOut,
)

router = APIRouter(prefix="/quality-results", tags=["质检结果"])


def _extract_recording_id(file_name: str) -> str | None:
    if file_name.startswith("recording_") and file_name.endswith(".json"):
        return file_name.removeprefix("recording_").removesuffix(".json")
    return None


def _extract_metrics(result: dict | None) -> tuple[dict, list[QualityResultDimensionOut]]:
    data = normalize_analysis_result(result) or {}
    evaluation = data.get("consultation_evaluation") or {}
    demands = data.get("customer_demands") or {}
    concerns = data.get("customer_concerns") or {}
    profile = data.get("customer_profile") or {}

    focus_areas = [
        str(item.get("area") or "").strip()
        for item in (demands.get("focus_areas") or [])
        if isinstance(item, dict) and str(item.get("area") or "").strip()
    ]
    dimensions = [
        QualityResultDimensionOut(
            name=str(item.get("name") or "未命名维度"),
            score=float(item.get("score") or 0),
            comment=str(item.get("comment") or ""),
        )
        for item in (evaluation.get("dimensions") or [])
        if isinstance(item, dict)
    ]
    return (
        {
            "overall_score": float(evaluation.get("overall_score")) if evaluation.get("overall_score") is not None else None,
            "dialogue_type": demands.get("expectation", {}).get("dialogue_type"),
            "focus_areas": focus_areas,
            "concern_count": len(concerns.get("items") or []),
            "tag_count": len(profile.get("tags") or []),
            "dimension_count": len(dimensions),
            "customer_demands": demands or None,
            "customer_concerns": concerns or None,
            "customer_profile": profile or None,
            "consultation_evaluation": evaluation or None,
        },
        dimensions,
    )


def _quality_state(status: str, score: float | None) -> tuple[str, str]:
    if status in {"pending", "running"}:
        return "分析中", "processing"
    if status == "failed":
        return "分析失败", "error"
    if score is None:
        return "待分析", "default"
    if score >= 8:
        return "优秀", "success"
    if score >= 6.5:
        return "良好", "processing"
    if score >= 5:
        return "一般", "warning"
    return "待提升", "error"


async def _load_recording_map(db: AsyncSession, recording_ids: list[str]) -> dict[str, Recording]:
    if not recording_ids:
        return {}
    rows = (
        await db.execute(
            select(Recording)
            .where(Recording.id.in_(recording_ids))
            .options(
                selectinload(Recording.staff),
                selectinload(Recording.visit).selectinload(Visit.customer),
            )
        )
    ).scalars().all()
    return {item.id: item for item in rows}


def _to_summary(task: AnalysisTask, recording: Recording | None) -> tuple[QualityResultOut, list[QualityResultDimensionOut]]:
    metrics, dimensions = _extract_metrics(task.result)
    score = metrics["overall_score"]
    quality_label, quality_tone = _quality_state(task.status, score)
    visit = recording.visit if recording else None
    customer = visit.customer if visit and visit.customer else None
    staff = recording.staff if recording and recording.staff else None
    source_type = "recording" if recording else "uploaded_json"

    summary = QualityResultOut(
        id=task.id,
        file_name=task.file_name,
        status=task.status,
        source_type=source_type,
        quality_label=quality_label,
        quality_tone=quality_tone,
        overall_score=score,
        dialogue_type=metrics["dialogue_type"],
        focus_areas=metrics["focus_areas"],
        concern_count=metrics["concern_count"],
        tag_count=metrics["tag_count"],
        dimension_count=metrics["dimension_count"],
        recording_id=recording.id if recording else None,
        recording_name=recording.file_name if recording else None,
        recording_status=recording.status if recording else None,
        visit_id=recording.visit_id if recording else None,
        staff_id=recording.staff_id if recording else None,
        staff_name=staff.name if staff else None,
        staff_badge_id=staff.badge_id if staff else None,
        customer_id=customer.id if customer else None,
        customer_name=customer.name if customer else None,
        recorded_at=recording.created_at.isoformat() if recording and recording.created_at else None,
        created_at=task.created_at.isoformat() if task.created_at else "",
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
    )
    return summary, dimensions


@router.get("", response_model=PaginatedResponse[QualityResultOut])
async def list_quality_results(
    status_filter: Annotated[str, Query(alias="status")] = "done",
    keyword: Annotated[str | None, Query()] = None,
    staff_id: Annotated[str | None, Query()] = None,
    min_score: Annotated[float | None, Query(ge=0, le=10)] = None,
    max_score: Annotated[float | None, Query(ge=0, le=10)] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    sort_by: Annotated[str, Query(pattern="^(time|score)$")] = "time",
    sort_order: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    scope = await build_permission_scope(current_user)
    recording_task_file_name = literal("recording_") + Recording.id + literal(".json")
    stmt = (
        select(AnalysisTask, Recording)
        .join(Recording, AnalysisTask.file_name == recording_task_file_name)
        .outerjoin(Staff, Staff.id == Recording.staff_id)
        .outerjoin(Visit, Visit.id == Recording.visit_id)
        .outerjoin(Customer, Customer.id == Visit.customer_id)
        .where(recording_scope_condition(scope), Recording.status != "filtered")
        .options(
            selectinload(Recording.staff),
            selectinload(Recording.visit).selectinload(Visit.customer),
        )
    )
    if status_filter != "all":
        stmt = stmt.where(AnalysisTask.status == status_filter)
    if min_score is not None:
        stmt = stmt.where(AnalysisTask.overall_score.is_not(None), AnalysisTask.overall_score >= min_score)
    if max_score is not None:
        stmt = stmt.where(AnalysisTask.overall_score.is_not(None), AnalysisTask.overall_score <= max_score)
    if staff_id:
        stmt = stmt.where(Recording.staff_id == staff_id)
    if keyword:
        needle = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                AnalysisTask.file_name.ilike(needle),
                Recording.file_name.ilike(needle),
                Customer.name.ilike(needle),
                Staff.name.ilike(needle),
                Staff.badge_id.ilike(needle),
                Recording.visit_id.ilike(needle),
            )
        )

    # 把日期范围和排序下推到 SQL，避免一次性加载全量任务记录到 Python 内排序。
    effective_date_from = date_from
    if effective_date_from is None and date_to is None:
        # 用户未指定日期时，默认仅取最近 180 天的任务，限制扫描范围。
        effective_date_from = datetime.now(timezone.utc).date() - timedelta(days=180)
    if effective_date_from is not None:
        stmt = stmt.where(
            AnalysisTask.created_at >= datetime.combine(effective_date_from, time.min, tzinfo=timezone.utc)
        )
    if date_to is not None:
        stmt = stmt.where(
            AnalysisTask.created_at < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
        )

    total = (
        await db.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))
    ).scalar_one()
    if total == 0:
        return make_page_response([], 0, page, page_size)

    if sort_by == "score":
        score_col = AnalysisTask.overall_score
        stmt = stmt.order_by(score_col.desc().nullslast() if sort_order == "desc" else score_col.asc().nullsfirst())
    else:  # time
        time_col = func.coalesce(AnalysisTask.completed_at, AnalysisTask.created_at)
        stmt = stmt.order_by(time_col.desc().nullslast() if sort_order == "desc" else time_col.asc().nullsfirst())

    rows = (
        await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    ).all()

    items: list[QualityResultOut] = []
    for task, recording in rows:
        summary, _ = _to_summary(task, recording)
        items.append(summary)

    return make_page_response(items, total, page, page_size)


@router.get("/{task_id}", response_model=QualityResultDetailOut)
async def get_quality_result(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await db.get(AnalysisTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="质检结果不存在")
    await ensure_task_visible(task, db, current_user, not_found_detail="质检结果不存在")

    recording_id = _extract_recording_id(task.file_name)
    recording_map = await _load_recording_map(db, [recording_id] if recording_id else [])
    recording = recording_map.get(recording_id or "")
    summary, dimensions = _to_summary(task, recording)
    metrics, _ = _extract_metrics(task.result)

    return QualityResultDetailOut(
        **summary.model_dump(),
        error_message=task.error_message,
        duration_ms=task.duration_ms,
        segment_count=task.segment_count,
        dimensions=dimensions,
        customer_demands=metrics["customer_demands"],
        customer_concerns=metrics["customer_concerns"],
        customer_profile=metrics["customer_profile"],
        consultation_evaluation=metrics["consultation_evaluation"],
    )
