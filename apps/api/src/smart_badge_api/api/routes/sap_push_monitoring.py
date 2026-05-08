from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.db.models import SapPushLog, VisitOrder
from smart_badge_api.db.session import get_db
from smart_badge_api.sap_push_service import serialize_sap_push_log
from smart_badge_api.schemas.pagination import PaginatedResponse, make_page_response
from smart_badge_api.schemas.sap_push_monitoring import SapPushMonitoringLogOut, SapPushMonitoringOverviewOut

router = APIRouter(prefix="/sap-push-monitoring", tags=["SAP回传监控"])


def _split_visit_order_ref(raw_ref: str | None) -> tuple[str | None, str | None]:
    normalized_ref = str(raw_ref or "").strip()
    if not normalized_ref:
        return None, None
    if "-" not in normalized_ref:
        return normalized_ref, None

    visit_order_no, visit_order_seg = normalized_ref.rsplit("-", 1)
    normalized_no = visit_order_no.strip()
    normalized_seg = visit_order_seg.strip()
    return normalized_no or normalized_ref, normalized_seg or None


def _normalize_response_attempt_groups(response_items: list[dict[str, Any]] | None) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for default_index, raw_item in enumerate(response_items or [], start=1):
        if not isinstance(raw_item, dict):
            continue
        try:
            request_index = int(raw_item.get("request_index") or default_index)
        except (TypeError, ValueError):
            request_index = default_index
        try:
            attempt = int(raw_item.get("attempt") or 1)
        except (TypeError, ValueError):
            attempt = 1

        item = dict(raw_item)
        item["request_index"] = request_index
        item["attempt"] = attempt
        grouped.setdefault(request_index, []).append(item)

    for attempts in grouped.values():
        attempts.sort(key=lambda item: int(item.get("attempt") or 1))
    return grouped


def _iter_monitoring_target_refs(log: SapPushLog) -> list[tuple[str, str | None]]:
    refs: list[tuple[str, str | None]] = []
    seen_refs: set[tuple[str, str | None]] = set()

    for payload in list(log.request_payloads or []):
        if not isinstance(payload, dict):
            continue
        zxxx = payload.get("zxxx")
        if not isinstance(zxxx, dict):
            continue
        visit_order_no, visit_order_seg = _split_visit_order_ref(zxxx.get("fzdh"))
        if not visit_order_no:
            continue
        ref = (visit_order_no, visit_order_seg)
        if ref in seen_refs:
            continue
        seen_refs.add(ref)
        refs.append(ref)

    fallback_ref = (
        str(log.visit_order_no or "").strip() or None,
        str(log.visit_order_seg or "").strip() or None,
    )
    if fallback_ref[0]:
        normalized_fallback = (fallback_ref[0], fallback_ref[1] or None)
        if normalized_fallback not in seen_refs:
            refs.append(normalized_fallback)

    return refs


async def _load_visit_order_lookup(
    db: AsyncSession,
    logs: list[SapPushLog],
) -> dict[tuple[str, str | None], VisitOrder]:
    refs: list[tuple[str, str | None]] = []
    seen_refs: set[tuple[str, str | None]] = set()
    for log in logs:
        for ref in _iter_monitoring_target_refs(log):
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            refs.append(ref)

    if not refs:
        return {}

    conditions = [
        and_(VisitOrder.dzdh == visit_order_no, VisitOrder.dzseg == visit_order_seg)
        if visit_order_seg is not None
        else and_(VisitOrder.dzdh == visit_order_no, VisitOrder.dzseg.is_(None))
        for visit_order_no, visit_order_seg in refs
    ]
    visit_orders = (await db.execute(select(VisitOrder).where(or_(*conditions)))).scalars().all()
    return {
        (item.dzdh, item.dzseg): item
        for item in visit_orders
    }


def _to_monitoring_log_rows(
    log: SapPushLog,
    *,
    visit_order_lookup: dict[tuple[str, str | None], VisitOrder],
) -> list[SapPushMonitoringLogOut]:
    data = serialize_sap_push_log(log)
    response_attempt_groups = _normalize_response_attempt_groups(list(data.get("response_items") or []))
    payloads = [item for item in list(data.get("request_payloads") or []) if isinstance(item, dict)]
    target_count = max(
        len(payloads),
        max(response_attempt_groups.keys(), default=0),
        1,
    )
    rows: list[SapPushMonitoringLogOut] = []

    base_visit_order_no = str(data.get("visit_order_no") or "").strip() or None
    base_visit_order_seg = str(data.get("visit_order_seg") or "").strip() or None
    base_result_status = str(data.get("effective_status") or data.get("status") or "prepared")
    base_result_reason = str(data.get("effective_reason") or data.get("error_message") or "").strip() or None

    for target_index in range(1, target_count + 1):
        payload = payloads[target_index - 1] if target_index - 1 < len(payloads) else None
        zxxx = payload.get("zxxx") if isinstance(payload, dict) and isinstance(payload.get("zxxx"), dict) else {}
        visit_order_no, visit_order_seg = _split_visit_order_ref(zxxx.get("fzdh"))
        target_ref = (visit_order_no, visit_order_seg) if visit_order_no else None
        visit_order = visit_order_lookup.get(target_ref) if target_ref else None
        attempts = list(response_attempt_groups.get(target_index, []))
        final_attempt = attempts[-1] if attempts else None

        if final_attempt is not None:
            result_status = "succeeded" if bool(final_attempt.get("success")) else "failed"
            business_status = str(final_attempt.get("business_status") or "").strip() or None
            business_message = str(final_attempt.get("business_message") or "").strip() or None
            result_reason = business_message
            http_status_code = final_attempt.get("http_status_code")
        else:
            result_status = base_result_status
            business_status = str(data.get("effective_business_status") or data.get("business_status") or "").strip() or None
            business_message = str(data.get("business_message") or "").strip() or None
            result_reason = base_result_reason
            http_status_code = data.get("http_status_code")

        matched_primary_target = (
            bool(base_visit_order_no)
            and visit_order_no == base_visit_order_no
            and (visit_order_seg or None) == (base_visit_order_seg or None)
        )
        customer_name = (
            (visit_order.ninam if visit_order else None)
            or (data.get("customer_name") if matched_primary_target else None)
        )
        customer_code = (
            (visit_order.kunr if visit_order else None)
            or str(zxxx.get("kunr") or "").strip()
            or (data.get("customer_code") if matched_primary_target else None)
        )
        advisor_name = (
            (visit_order.advxc_long if visit_order else None)
            or (data.get("advisor_name") if matched_primary_target or target_count == 1 else None)
        )
        visit_id = data.get("visit_id") if matched_primary_target or target_count == 1 else None

        rows.append(
            SapPushMonitoringLogOut(
                **{
                    **data,
                    "id": f"{data['id']}:{target_index}",
                    "visit_id": visit_id,
                    "visit_order_no": visit_order_no or (base_visit_order_no if target_count == 1 else None),
                    "visit_order_seg": visit_order_seg or (base_visit_order_seg if target_count == 1 else None),
                    "customer_name": customer_name,
                    "customer_code": customer_code,
                    "advisor_name": advisor_name,
                    "status": result_status,
                    "request_payloads": [payload] if payload is not None else [],
                    "response_items": attempts,
                    "http_status_code": http_status_code,
                    "business_status": business_status,
                    "business_message": business_message,
                    "effective_status": result_status,
                    "effective_business_status": business_status,
                    "effective_reason": result_reason,
                },
                log_id=data["id"],
                target_index=target_index,
                target_count=target_count,
                is_primary_target=(target_index == 1),
                result_status=result_status,
                result_reason=result_reason,
            )
        )

    return rows


def _matches_keyword(row: SapPushMonitoringLogOut, keyword: str) -> bool:
    normalized_keyword = keyword.strip().lower()
    if not normalized_keyword:
        return True

    searchable_values = [
        row.log_id,
        row.recording_id,
        row.recording_file_name,
        row.visit_order_no,
        row.visit_order_seg,
        row.customer_name,
        row.customer_code,
        row.advisor_name,
        row.result_reason,
        f"{row.visit_order_no}-{row.visit_order_seg}" if row.visit_order_no and row.visit_order_seg else None,
    ]
    return any(
        normalized_keyword in str(value).lower()
        for value in searchable_values
        if value
    )


@router.get("/overview", response_model=SapPushMonitoringOverviewOut)
async def get_sap_push_monitoring_overview(
    db: AsyncSession = Depends(get_db),
):
    logs = (
        await db.execute(
            select(SapPushLog)
            .options(selectinload(SapPushLog.recording))
            .order_by(desc(SapPushLog.created_at))
        )
    ).scalars().all()

    visit_order_lookup = await _load_visit_order_lookup(db, logs)
    rows = [
        row
        for log in logs
        for row in _to_monitoring_log_rows(log, visit_order_lookup=visit_order_lookup)
    ]

    total_count = len(rows)
    succeeded_count = 0
    failed_count = 0
    pending_count = 0
    auto_count = 0
    manual_count = 0
    latest_sent_at = None

    for row in rows:
        if row.result_status == "succeeded":
            succeeded_count += 1
        elif row.result_status == "failed":
            failed_count += 1
        else:
            pending_count += 1

        if str(row.trigger_mode or "").strip() == "manual":
            manual_count += 1
        else:
            auto_count += 1

    for log in logs:
        if log.sent_at and latest_sent_at is None:
            latest_sent_at = log.sent_at.isoformat()

    return SapPushMonitoringOverviewOut(
        total_count=total_count,
        succeeded_count=succeeded_count,
        failed_count=failed_count,
        pending_count=pending_count,
        auto_count=auto_count,
        manual_count=manual_count,
        latest_sent_at=latest_sent_at,
    )


@router.get("/logs", response_model=PaginatedResponse[SapPushMonitoringLogOut])
async def list_sap_push_monitoring_logs(
    db: AsyncSession = Depends(get_db),
    status: str = Query(default="all"),
    trigger_mode: str = Query(default="all"),
    keyword: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    stmt = select(SapPushLog).options(selectinload(SapPushLog.recording)).order_by(desc(SapPushLog.created_at))
    if trigger_mode and trigger_mode != "all":
        stmt = stmt.where(SapPushLog.trigger_mode == trigger_mode)
    # 若用户未指定起始日期，默认仅查询最近 90 天，避免一次性加载全部历史。
    effective_date_from = date_from
    if effective_date_from is None:
        effective_date_from = (datetime.now(timezone.utc).date() - timedelta(days=90))
    stmt = stmt.where(SapPushLog.created_at >= datetime.combine(effective_date_from, time.min, tzinfo=timezone.utc))
    if date_to:
        stmt = stmt.where(
            SapPushLog.created_at < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
        )

    logs = (await db.execute(stmt)).scalars().all()
    visit_order_lookup = await _load_visit_order_lookup(db, logs)
    rows = [
        row
        for log in logs
        for row in _to_monitoring_log_rows(log, visit_order_lookup=visit_order_lookup)
    ]
    if keyword and keyword.strip():
        rows = [row for row in rows if _matches_keyword(row, keyword)]
    if status and status != "all":
        rows = [row for row in rows if row.result_status == status]

    total = len(rows)
    start = (page - 1) * page_size
    end = start + page_size
    return make_page_response(rows[start:end], total, page, page_size)
