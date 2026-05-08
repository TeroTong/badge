"""对话片段管理路由 — 查看片段、关联到诊单、手动触发拆分。"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.data_scope import build_permission_scope, recording_scope_condition, visit_scope_condition
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.db.models import Recording, Segment, Transcript, User, Visit
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.pagination import PaginatedResponse, make_page_response
from smart_badge_api.schemas.segments import SegmentOut, SegmentUpdate

router = APIRouter(prefix="/segments", tags=["对话片段"])


def _to_out(s: Segment) -> SegmentOut:
    return SegmentOut(
        id=s.id,
        recording_id=s.recording_id,
        visit_id=s.visit_id,
        segment_index=s.segment_index,
        begin_ms=s.begin_ms,
        end_ms=s.end_ms,
        speaker_label=s.speaker_label,
        text=s.text,
        status=s.status,
        has_analysis=bool(s.analysis_result),
        created_at=s.created_at.isoformat() if s.created_at else "",
    )


async def _get_scoped_segment(segment_id: str, db: AsyncSession, current_user: User) -> Segment | None:
    scope = await build_permission_scope(current_user)
    return (
        await db.execute(
            select(Segment)
            .join(Recording, Segment.recording_id == Recording.id)
            .where(
                Segment.id == segment_id,
                recording_scope_condition(scope),
            )
        )
    ).scalar_one_or_none()


async def _get_scoped_recording(recording_id: str, db: AsyncSession, current_user: User) -> Recording | None:
    scope = await build_permission_scope(current_user)
    return (
        await db.execute(
            select(Recording).where(
                Recording.id == recording_id,
                recording_scope_condition(scope),
            )
        )
    ).scalar_one_or_none()


async def _visit_visible(visit_id: str, db: AsyncSession, current_user: User) -> bool:
    scope = await build_permission_scope(current_user)
    return (
        await db.execute(
            select(Visit.id).where(
                Visit.id == visit_id,
                visit_scope_condition(scope),
            )
        )
    ).scalar_one_or_none() is not None


@router.get("", response_model=PaginatedResponse[SegmentOut])
async def list_segments(
    recording_id: str | None = Query(None),
    visit_id: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    scope = await build_permission_scope(current_user)
    stmt = (
        select(Segment)
        .join(Recording, Segment.recording_id == Recording.id)
        .where(recording_scope_condition(scope))
        .order_by(Segment.recording_id, Segment.segment_index)
    )
    if recording_id:
        stmt = stmt.where(Segment.recording_id == recording_id)
    if visit_id:
        stmt = stmt.where(Segment.visit_id == visit_id)
    if status:
        stmt = stmt.where(Segment.status == status)
    total: int = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()
    return make_page_response([_to_out(s) for s in rows], total, page, page_size)


@router.get("/{segment_id}", response_model=SegmentOut)
async def get_segment(
    segment_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    s = await _get_scoped_segment(segment_id, db, current_user)
    if not s:
        raise HTTPException(404, "片段不存在")
    return _to_out(s)


@router.put("/{segment_id}", response_model=SegmentOut)
async def update_segment(
    segment_id: str,
    body: SegmentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新片段（主要用于关联到诊单、修改说话人标签）。"""
    segment = await _get_scoped_segment(segment_id, db, current_user)
    if not segment:
        raise HTTPException(404, "片段不存在")

    updates = body.model_dump(exclude_unset=True)
    if "visit_id" in updates and updates["visit_id"]:
        if not await _visit_visible(updates["visit_id"], db, current_user):
            raise HTTPException(400, "到诊单不存在")
    for key, value in updates.items():
        setattr(segment, key, value)
    await db.commit()
    return _to_out(segment)


@router.post("/{segment_id}/unlink", response_model=SegmentOut)
async def unlink_segment_from_visit(
    segment_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """取消片段与到诊单的关联。"""
    segment = await _get_scoped_segment(segment_id, db, current_user)
    if not segment:
        raise HTTPException(404, "片段不存在")
    segment.visit_id = None
    await db.commit()
    return _to_out(segment)


@router.post("/resplit/{recording_id}", response_model=list[SegmentOut])
async def resplit_segments(
    recording_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """重新触发某段录音的片段拆分。"""
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "录音不存在")

    transcript = (await db.execute(
        select(Transcript).where(
            Transcript.recording_id == recording_id,
            Transcript.status == "completed",
        )
    )).scalar_one_or_none()
    if not transcript:
        raise HTTPException(400, "该录音尚无已完成的转写结果，请先触发转写")

    from smart_badge_api.asr.service import execute_segmentation
    await execute_segmentation(recording_id)

    # execute_segmentation 使用独立 session，需要 expire 本 session 缓存
    db.expire_all()

    rows = (await db.execute(
        select(Segment)
        .where(Segment.recording_id == recording_id)
        .order_by(Segment.segment_index)
    )).scalars().all()
    return [_to_out(s) for s in rows]
