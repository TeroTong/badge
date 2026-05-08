from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.db.models import AuditLog
from smart_badge_api.db.session import get_db
from smart_badge_api.db.system_defaults import ensure_system_audit_logs
from smart_badge_api.schemas.audit_logs import AuditLogOut
from smart_badge_api.schemas.pagination import PaginatedResponse, make_page_response

router = APIRouter(prefix="/audit-logs", tags=["操作日志"])

SAP_HANA_PUSH_AUDIT_OPERATOR = "SAP HANA"
SAP_HANA_PUSH_AUDIT_ACTION = "SAP HANA 推送到诊分诊单"


def _to_out(item: AuditLog) -> AuditLogOut:
    return AuditLogOut(
        id=item.id,
        operator_name=item.operator_name,
        ip_address=item.ip_address,
        module_name=item.module_name,
        action_name=item.action_name,
        content=item.content,
        created_at=item.created_at.isoformat() if item.created_at else "",
    )


@router.get("", response_model=PaginatedResponse[AuditLogOut])
async def list_audit_logs(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    ip_address: str | None = Query(default=None),
    module_name: str | None = Query(default=None),
    content: str | None = Query(default=None),
    operator_name: str | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    await ensure_system_audit_logs(db)
    stmt = (
        select(AuditLog)
        .where(
            ~(
                (AuditLog.operator_name == SAP_HANA_PUSH_AUDIT_OPERATOR)
                & (AuditLog.action_name == SAP_HANA_PUSH_AUDIT_ACTION)
            )
        )
        .order_by(AuditLog.created_at.desc())
    )
    if date_from:
        start_dt = datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc)
        stmt = stmt.where(AuditLog.created_at >= start_dt)
    if date_to:
        end_dt = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        stmt = stmt.where(AuditLog.created_at < end_dt)
    if ip_address:
        stmt = stmt.where(AuditLog.ip_address.ilike(f"%{ip_address.strip()}%"))
    if module_name:
        stmt = stmt.where(AuditLog.module_name.ilike(f"%{module_name.strip()}%"))
    if content:
        stmt = stmt.where(AuditLog.content.ilike(f"%{content.strip()}%"))
    if operator_name:
        stmt = stmt.where(AuditLog.operator_name.ilike(f"%{operator_name.strip()}%"))

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return make_page_response([_to_out(item) for item in rows], total, page, page_size)
