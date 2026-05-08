from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, exists, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from smart_badge_api.api.data_scope import build_permission_scope, recording_scope_condition
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.core.permissions import is_global_role, permission_role_level
from smart_badge_api.db.models import Customer, Recording, RiskRecord, Staff
from smart_badge_api.db.models import User
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.pagination import PaginatedResponse, make_page_response
from smart_badge_api.schemas.risk import (
    RiskRecordDetailOut,
    RiskRecordOut,
    RiskRecordOverviewOut,
    RiskRecordStatusUpdate,
)

router = APIRouter(prefix="/risk-records", tags=["risk-records"])


def _require_hospital_admin_or_above(user: User) -> None:
    if permission_role_level(user.role) < permission_role_level("hospital_admin"):
        raise HTTPException(403, "权限不足，需要至少 hospital_admin 角色")


def _risk_record_scope_condition(scope):
    if is_global_role(scope.role):
        return true()
    return exists(
        select(Recording.id).where(
            Recording.id == RiskRecord.recording_id,
            recording_scope_condition(scope),
        )
    )


def _opts():
    return [
        selectinload(RiskRecord.recording),
        selectinload(RiskRecord.staff),
        selectinload(RiskRecord.customer),
        selectinload(RiskRecord.task),
    ]


def _to_out(item: RiskRecord) -> RiskRecordOut:
    return RiskRecordOut(
        id=item.id,
        rule_id=item.rule_id,
        task_id=item.task_id,
        recording_id=item.recording_id,
        recording_name=item.recording.file_name if item.recording else None,
        visit_id=item.visit_id,
        staff_id=item.staff_id,
        staff_name=item.staff.name if item.staff else None,
        staff_badge_id=item.staff.badge_id if item.staff else None,
        customer_id=item.customer_id,
        customer_name=item.customer.name if item.customer else None,
        source_type=item.source_type,
        rule_name=item.rule_name,
        risk_label=item.risk_label,
        severity=item.severity,
        status=item.status,
        matched_dimension_name=item.matched_dimension_name,
        matched_keywords=[str(keyword) for keyword in (item.matched_keywords or [])],
        overall_score=item.overall_score,
        summary=item.summary,
        hit_excerpt=item.hit_excerpt,
        created_at=item.created_at.isoformat() if item.created_at else "",
        resolved_at=item.resolved_at.isoformat() if item.resolved_at else None,
    )


@router.get("/overview", response_model=RiskRecordOverviewOut)
async def get_risk_record_overview(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_hospital_admin_or_above(current_user)
    scope = await build_permission_scope(current_user)
    rows = (
        await db.execute(
            select(RiskRecord.severity, RiskRecord.status).where(_risk_record_scope_condition(scope))
        )
    ).all()

    severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    status_counts = {"open": 0, "resolved": 0, "ignored": 0}
    for severity, status in rows:
        if severity in severity_counts:
            severity_counts[severity] += 1
        if status in status_counts:
            status_counts[status] += 1

    return RiskRecordOverviewOut(
        total=len(rows),
        open_count=status_counts["open"],
        resolved_count=status_counts["resolved"],
        ignored_count=status_counts["ignored"],
        critical_count=severity_counts["critical"],
        high_count=severity_counts["high"],
        medium_count=severity_counts["medium"],
        low_count=severity_counts["low"],
    )


@router.get("", response_model=PaginatedResponse[RiskRecordOut])
async def list_risk_records(
    keyword: Annotated[str | None, Query()] = None,
    staff_id: Annotated[str | None, Query()] = None,
    severity: Annotated[str | None, Query(pattern="^(low|medium|high|critical)$")] = None,
    status_filter: Annotated[str | None, Query(alias="status", pattern="^(open|resolved|ignored)$")] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_hospital_admin_or_above(current_user)
    scope = await build_permission_scope(current_user)
    staff_join = aliased(Staff)
    customer_join = aliased(Customer)
    recording_join = aliased(Recording)

    stmt = (
        select(RiskRecord)
        .outerjoin(staff_join, RiskRecord.staff_id == staff_join.id)
        .outerjoin(customer_join, RiskRecord.customer_id == customer_join.id)
        .outerjoin(recording_join, RiskRecord.recording_id == recording_join.id)
        .where(_risk_record_scope_condition(scope))
        .order_by(RiskRecord.created_at.desc())
    )

    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                RiskRecord.rule_name.ilike(like),
                RiskRecord.risk_label.ilike(like),
                RiskRecord.summary.ilike(like),
                RiskRecord.hit_excerpt.ilike(like),
                recording_join.file_name.ilike(like),
                staff_join.name.ilike(like),
                staff_join.badge_id.ilike(like),
                customer_join.name.ilike(like),
                cast(RiskRecord.visit_id, String).ilike(like),
            )
        )
    if staff_id:
        stmt = stmt.where(RiskRecord.staff_id == staff_id)
    if severity:
        stmt = stmt.where(RiskRecord.severity == severity)
    if status_filter:
        stmt = stmt.where(RiskRecord.status == status_filter)
    if date_from:
        start_dt = datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc)
        stmt = stmt.where(RiskRecord.created_at >= start_dt)
    if date_to:
        end_dt = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        stmt = stmt.where(RiskRecord.created_at < end_dt)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(stmt.options(*_opts()).offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    return make_page_response([_to_out(item) for item in rows], total, page, page_size)


@router.get("/{record_id}", response_model=RiskRecordDetailOut)
async def get_risk_record(
    record_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_hospital_admin_or_above(current_user)
    scope = await build_permission_scope(current_user)
    item = (
        await db.execute(
            select(RiskRecord)
            .where(RiskRecord.id == record_id, _risk_record_scope_condition(scope))
            .options(*_opts())
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Risk record not found")

    summary = _to_out(item)
    return RiskRecordDetailOut(
        **summary.model_dump(),
        evidence=item.evidence or None,
        recording_status=item.recording.status if item.recording else None,
        task_status=item.task.status if item.task else None,
        task_completed_at=item.task.completed_at.isoformat() if item.task and item.task.completed_at else None,
    )


@router.put("/{record_id}/status", response_model=RiskRecordOut)
async def update_risk_record_status(
    record_id: str,
    body: RiskRecordStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_hospital_admin_or_above(current_user)
    scope = await build_permission_scope(current_user)
    item = (
        await db.execute(
            select(RiskRecord)
            .where(RiskRecord.id == record_id, _risk_record_scope_condition(scope))
            .options(*_opts())
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Risk record not found")

    item.status = body.status
    item.resolved_at = datetime.now(timezone.utc) if body.status == "resolved" else None
    await db.commit()
    refreshed = (
        await db.execute(
            select(RiskRecord)
            .where(RiskRecord.id == record_id, _risk_record_scope_condition(scope))
            .options(*_opts())
        )
    ).scalar_one_or_none()
    if refreshed is None:
        raise HTTPException(status_code=404, detail="Risk record not found")
    return _to_out(refreshed)
