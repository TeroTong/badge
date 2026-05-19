"""到诊单 API 路由。"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, false, func, select, true, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.data_scope import (
    build_permission_scope,
    visit_order_scope_condition,
    visit_scope_condition,
)
from smart_badge_api.api.deps import get_current_user, get_db
from smart_badge_api.api.hospital_scope import normalize_hospital_code
from smart_badge_api.customer_type import customer_type_from_visit_order
from smart_badge_api.db.models import Recording, RecordingVisitLink, SapHanaVisitOrder, Staff, User, Visit, VisitOrder
from smart_badge_api.schemas.matching import VisitOrderRecordingMatchOut
from smart_badge_api.schemas.visit_order import VisitOrderOut, VisitOrderSyncResult
from smart_badge_api.visit_order_sync import sync_visit_orders_for_context, sync_visit_orders
from smart_badge_api.visit_order_matching import (
    _department_assistant_order_match,
    _extract_companion_customer_codes,
    _find_companion_orders,
    _is_department_assistant_staff,
    _load_staff_position_text,
    _visit_order_ref,
    analyze_visit_order_recording_match,
)
from smart_badge_api.api.audit import append_audit_log

router = APIRouter(prefix="/visit-orders", tags=["visit-orders"])
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _clean_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _visit_order_sap_key(vo: VisitOrder) -> tuple[str, str] | None:
    hospital_code = _clean_text(vo.jgbm)
    visit_order_no = _clean_text(vo.dzdh)
    if not hospital_code or not visit_order_no:
        return None
    return hospital_code, visit_order_no


def _extract_department_advisor(payload: object) -> dict[str, str | None]:
    if not isinstance(payload, dict):
        return {"ksgw": None, "ksgw_long": None}
    return {
        "ksgw": _clean_text(payload.get("KSGW") or payload.get("ksgw")),
        "ksgw_long": _clean_text(payload.get("KSGW_LONG") or payload.get("ksgw_long")),
    }


async def _load_department_advisor_map(
    db: AsyncSession,
    visit_orders: list[VisitOrder],
) -> dict[tuple[str, str], dict[str, str | None]]:
    keys = sorted({key for vo in visit_orders if (key := _visit_order_sap_key(vo)) is not None})
    if not keys:
        return {}

    rows = (
        await db.execute(
            select(SapHanaVisitOrder.jgbm, SapHanaVisitOrder.dzdh, SapHanaVisitOrder.source_payload).where(
                tuple_(SapHanaVisitOrder.jgbm, SapHanaVisitOrder.dzdh).in_(keys)
            )
        )
    ).all()
    return {
        (_clean_text(jgbm) or "", _clean_text(dzdh) or ""): _extract_department_advisor(source_payload)
        for jgbm, dzdh, source_payload in rows
    }


def _to_out(vo: VisitOrder, department_advisor: dict[str, str | None] | None = None) -> VisitOrderOut:
    data = {
        field_name: getattr(vo, field_name, None)
        for field_name in VisitOrderOut.model_fields
    }
    if department_advisor:
        data.update(department_advisor)
    return VisitOrderOut(**data)


def _resolve_local_visit_for_order(
    order: VisitOrder,
    visit_map: dict[tuple[str | None, str | None], Visit],
    visit_by_dzdh: dict[str, Visit],
) -> Visit | None:
    return visit_map.get((order.dzdh, order.dzseg)) or visit_by_dzdh.get(order.dzdh)


def _unique_ids(values: Iterable[str | None]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        items.append(normalized)
    return items


def _daily_visit_order_scope_condition(current_user: User):
    hospital_code = str(getattr(current_user, "hospital_code", "") or "").strip()
    if not hospital_code:
        return false()
    return VisitOrder.jgbm == hospital_code


def _pick_daily_visit_order_hospital_code(*codes: str | None) -> str | None:
    for code in codes:
        normalized = str(code or "").strip()
        if normalized:
            return normalized
    return None


def _daily_visit_order_matches_staff(visit_order: VisitOrder, staff: Staff | None) -> bool:
    staff_code = str(getattr(staff, "external_account", "") or "").strip()
    if not staff_code:
        return False
    return staff_code in {
        visit_order.fzuer,
        visit_order.d_fzuer,
        visit_order.fzr_id_dq,
        visit_order.advxc,
        visit_order.assxc,
        visit_order.advyq,
        visit_order.yyuer,
        visit_order.vipkf,
        visit_order.d_vipkf,
    }


def _daily_visit_order_matches_self_scope(
    visit_order: VisitOrder,
    staff: Staff | None,
    staff_position_text: str | None,
) -> bool:
    return _daily_visit_order_matches_staff(visit_order, staff) or _department_assistant_order_match(
        staff,
        staff_position_text,
        visit_order,
    )


def _daily_visit_order_matches_keyword(visit_order: VisitOrder, keyword: str | None) -> bool:
    normalized_keyword = str(keyword or "").strip()
    if not normalized_keyword:
        return True
    return any(
        normalized_keyword in str(value or "")
        for value in (
            visit_order.dzdh,
            visit_order.ninam,
            visit_order.kunr,
            visit_order.fzuer,
            visit_order.fzuer_long,
            visit_order.fzr_id_dq,
            visit_order.fzr_name_dq,
            visit_order.advxc_long,
            visit_order.remark_dz,
        )
    )


def _derive_recording_date_candidates(recording) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str | None):
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append(normalized)

    if recording.created_at:
        # User-facing and SAP matching dates are business dates in Asia/Shanghai.
        # Timestamps may be stored as UTC, so convert before extracting the date.
        add(_business_date_from_datetime(recording.created_at))

    file_name = str(getattr(recording, "file_name", "") or "").strip()
    if file_name:
        import re

        match_full = re.search(r"(\d{4})(\d{2})(\d{2})", file_name)
        if match_full:
            add(f"{match_full.group(1)}-{match_full.group(2)}-{match_full.group(3)}")
        else:
            match_mmdd = re.match(r"^(\d{2})(\d{2})_\d{6}(?:\.[A-Za-z0-9]+)?$", file_name)
            if match_mmdd:
                year = recording.created_at.year if getattr(recording, "created_at", None) else datetime.now().year
                add(f"{year:04d}-{match_mmdd.group(1)}-{match_mmdd.group(2)}")

    return candidates


def _business_date_from_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.date().isoformat()
    return value.astimezone(_BUSINESS_TZ).date().isoformat()


async def _list_visit_order_scope_condition(db: AsyncSession, scope):
    from smart_badge_api.core.permissions import normalize_permission_role
    role = normalize_permission_role(scope.role)
    if role == "super_admin":
        return true()
    return visit_order_scope_condition(scope)


def _build_daily_visit_order_items(
    visit_orders: list[VisitOrder],
    visits: list[Visit],
    *,
    recording_id: str,
    accessible_visit_ids: set[str] | None = None,
) -> list[dict[str, object]]:
    visit_map: dict[tuple[str | None, str | None], Visit] = {}
    visit_by_dzdh: dict[str, Visit] = {}
    local_visit_ids_by_dzdh: dict[str, list[str]] = {}
    linked_recording_names_by_dzdh: dict[str, list[str]] = {}

    for visit in visits:
        dzdh = visit.external_visit_order_no
        if not dzdh:
            continue
        visit_map[(visit.external_visit_order_no, visit.external_visit_order_seg)] = visit
        visit_by_dzdh.setdefault(dzdh, visit)
        local_visit_ids_by_dzdh.setdefault(dzdh, [])
        if visit.id not in local_visit_ids_by_dzdh[dzdh]:
            local_visit_ids_by_dzdh[dzdh].append(visit.id)
        linked_names = [
            link.recording.file_name
            for link in visit.recording_links
            if link.recording_id != recording_id and link.recording and link.recording.file_name
        ]
        if linked_names:
            linked_recording_names_by_dzdh.setdefault(dzdh, []).extend(linked_names)

    items: list[dict[str, object]] = []
    for visit_order in visit_orders:
        local_visit = _resolve_local_visit_for_order(visit_order, visit_map, visit_by_dzdh)
        local_visit_id = local_visit.id if local_visit else None
        detail_local_visit_id = (
            local_visit_id
            if local_visit_id and (accessible_visit_ids is None or local_visit_id in accessible_visit_ids)
            else None
        )
        associated_local_visit_ids = [
            visit_id
            for visit_id in local_visit_ids_by_dzdh.get(visit_order.dzdh, [])
            if visit_id != local_visit_id
        ]

        companion_orders = _find_companion_orders(visit_order, visit_orders)
        companion_visit_order_refs = [
            ref
            for ref in (_visit_order_ref(item) for item in companion_orders)
            if ref
        ]
        companion_local_visit_ids = _unique_ids(
            _resolve_local_visit_for_order(item, visit_map, visit_by_dzdh).id
            if _resolve_local_visit_for_order(item, visit_map, visit_by_dzdh)
            else None
            for item in companion_orders
        )

        customer_type_code, customer_type_label = customer_type_from_visit_order(visit_order)
        items.append({
            "id": visit_order.id,
            "dzdh": visit_order.dzdh,
            "dzseg": visit_order.dzseg,
            "ninam": visit_order.ninam,
            "kunr": visit_order.kunr,
            "customer_type_code": customer_type_code,
            "customer_type_label": customer_type_label,
            "sjrq": visit_order.sjrq,
            "fzsj": visit_order.fzsj,
            "fzuer": visit_order.fzuer or visit_order.fzr_id_dq,
            "fzuer_long": visit_order.fzuer_long or visit_order.fzr_name_dq,
            "advxc_long": visit_order.advxc_long,
            "jcsta_txt": visit_order.jcsta_txt,
            "remark_dz": visit_order.remark_dz,
            "linked_recording_names": linked_recording_names_by_dzdh.get(visit_order.dzdh, []),
            "local_visit_id": local_visit_id,
            "detail_local_visit_id": detail_local_visit_id,
            "associated_local_visit_ids": associated_local_visit_ids,
            "companion_local_visit_ids": companion_local_visit_ids,
            "companion_visit_order_refs": companion_visit_order_refs,
            "companion_customer_codes": _extract_companion_customer_codes(visit_order),
        })

    items.sort(key=lambda item: (1 if item["linked_recording_names"] else 0, item["fzsj"] or ""))
    return items


@router.get("", response_model=dict)
async def list_visit_orders(
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str | None = None,
    fzuer: str | None = None,
    hospital_code: str | None = None,
    sjrq_start: str | None = None,
    sjrq_end: str | None = None,
    jcsta_txt: str | None = None,
    fast_page: bool = Query(False),
    current_user: User = Depends(get_current_user),
):
    scope = await build_permission_scope(current_user)
    list_scope_condition = await _list_visit_order_scope_condition(db, scope)
    stmt = select(VisitOrder).where(list_scope_condition)
    count_stmt = select(func.count(VisitOrder.id)).where(list_scope_condition)
    requested_hospital_code = normalize_hospital_code(hospital_code)

    if keyword:
        stmt = stmt.where(
            VisitOrder.dzdh.contains(keyword)
            | VisitOrder.ninam.contains(keyword)
            | VisitOrder.advxc_long.contains(keyword)
            | VisitOrder.kunr.contains(keyword)
        )
        count_stmt = count_stmt.where(
            VisitOrder.dzdh.contains(keyword)
            | VisitOrder.ninam.contains(keyword)
            | VisitOrder.advxc_long.contains(keyword)
            | VisitOrder.kunr.contains(keyword)
        )

    if fzuer:
        stmt = stmt.where(VisitOrder.fzuer == fzuer)
        count_stmt = count_stmt.where(VisitOrder.fzuer == fzuer)

    if requested_hospital_code:
        stmt = stmt.where(VisitOrder.jgbm == requested_hospital_code)
        count_stmt = count_stmt.where(VisitOrder.jgbm == requested_hospital_code)

    if sjrq_start:
        stmt = stmt.where(VisitOrder.sjrq >= sjrq_start)
        count_stmt = count_stmt.where(VisitOrder.sjrq >= sjrq_start)

    if sjrq_end:
        stmt = stmt.where(VisitOrder.sjrq <= sjrq_end)
        count_stmt = count_stmt.where(VisitOrder.sjrq <= sjrq_end)

    if jcsta_txt:
        stmt = stmt.where(VisitOrder.jcsta_txt == jcsta_txt)
        count_stmt = count_stmt.where(VisitOrder.jcsta_txt == jcsta_txt)

    stmt = stmt.order_by(VisitOrder.sjrq.desc(), VisitOrder.fzsj.desc())
    if fast_page:
        page_items_with_probe = (
            await db.execute(stmt.offset((page - 1) * page_size).limit(page_size + 1))
        ).scalars().all()
        has_more = len(page_items_with_probe) > page_size
        items = page_items_with_probe[:page_size]
        total = page * page_size + 1 if has_more else (page - 1) * page_size + len(items)
    else:
        total = (await db.execute(count_stmt)).scalar() or 0
        items = (
            await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))
        ).scalars().all()

    department_advisor_map = await _load_department_advisor_map(db, items)
    return {
        "items": [
            _to_out(vo, department_advisor_map.get(_visit_order_sap_key(vo)))
            for vo in items
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/sync", response_model=VisitOrderSyncResult)
async def sync_visit_orders_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await sync_visit_orders(db)
    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="到诊单管理",
        action_name="同步到诊单数据",
        content=(
            f"到诊单同步：{result.date_range}，"
            f"共 {result.synced_count} 条，"
            f"新增 {result.new_count} 条，更新 {result.updated_count} 条"
        ),
    )
    return result



@router.get("/daily-for-recording/{recording_id}")
async def list_daily_visit_orders_for_recording(
    recording_id: str,
    scope_mode: str = "self",
    keyword: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return all visit orders on the same date as the recording, with linked-recording info."""
    from smart_badge_api.db.models import Recording
    from sqlalchemy.orm import selectinload

    recording = await db.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="录音不存在")
    rec_date_candidates = _derive_recording_date_candidates(recording)
    if not rec_date_candidates:
        return {"items": [], "recording_date": None}
    rec_date = rec_date_candidates[0]

    current_user_staff = await db.get(Staff, current_user.staff_id) if getattr(current_user, "staff_id", None) else None
    recording_staff = await db.get(Staff, recording.staff_id) if recording.staff_id else None
    resolved_hospital_code = _pick_daily_visit_order_hospital_code(
        getattr(current_user, "hospital_code", None),
        getattr(current_user_staff, "hospital_code", None),
        getattr(recording_staff, "hospital_code", None),
    )
    daily_scope_condition = VisitOrder.jgbm == resolved_hospital_code if resolved_hospital_code else false()
    scope = await build_permission_scope(current_user)
    normalized_scope_mode = "org" if scope_mode == "org" else "self"

    target_staff = recording_staff or current_user_staff
    target_staff_position_text = await _load_staff_position_text(db, target_staff)
    is_department_assistant = _is_department_assistant_staff(target_staff, target_staff_position_text)

    def _filter_daily_visit_orders(visit_orders: list[VisitOrder]) -> list[VisitOrder]:
        filtered_orders = visit_orders
        if normalized_scope_mode == "self":
            matched_visit_order_nos = {
                order.dzdh
                for order in filtered_orders
                if order.dzdh and _daily_visit_order_matches_self_scope(order, target_staff, target_staff_position_text)
            }
            filtered_orders = [order for order in filtered_orders if order.dzdh in matched_visit_order_nos]
        return [order for order in filtered_orders if _daily_visit_order_matches_keyword(order, keyword)]

    def _build_daily_visit_orders_stmt(date_field: str, target_date: str):
        field = VisitOrder.crtdt if date_field == "crtdt" else VisitOrder.sjrq
        conditions = [
            field == target_date,
            daily_scope_condition,
        ]
        return (
            select(VisitOrder)
            .where(
                and_(*conditions)
            )
            .order_by(VisitOrder.fzsj.asc())
        )

    vos: list[VisitOrder] = []
    resolved_rec_date: str | None = None
    for candidate_date in rec_date_candidates:
        stmt = _build_daily_visit_orders_stmt("crtdt", candidate_date)
        vos = _filter_daily_visit_orders((await db.execute(stmt)).scalars().all())
        if not vos:
            stmt = _build_daily_visit_orders_stmt("sjrq", candidate_date)
            vos = _filter_daily_visit_orders((await db.execute(stmt)).scalars().all())
        if vos:
            resolved_rec_date = candidate_date
            break

    advisor_code = str(getattr(recording_staff, "external_account", "") or "").strip()
    if not vos and resolved_hospital_code and (advisor_code or is_department_assistant):
        await sync_visit_orders_for_context(
            db,
            date_strings=set(rec_date_candidates),
            advisor_codes={advisor_code} if advisor_code else set(),
            hospital_codes={resolved_hospital_code},
        )
        for candidate_date in rec_date_candidates:
            stmt = _build_daily_visit_orders_stmt("crtdt", candidate_date)
            vos = _filter_daily_visit_orders((await db.execute(stmt)).scalars().all())
            if not vos:
                stmt = _build_daily_visit_orders_stmt("sjrq", candidate_date)
                vos = _filter_daily_visit_orders((await db.execute(stmt)).scalars().all())
            if vos:
                resolved_rec_date = candidate_date
                break

    if resolved_rec_date:
        rec_date = resolved_rec_date

    # Get all visits linked to these visit orders (to know which have recordings)
    dzdh_set = {vo.dzdh for vo in vos}
    visit_stmt = (
        select(Visit)
        .where(
            Visit.external_visit_order_no.in_(dzdh_set),
        )
        .options(selectinload(Visit.recording_links).selectinload(RecordingVisitLink.recording))
    )
    visits = (await db.execute(visit_stmt)).scalars().all()

    accessible_visit_ids = set(
        (
            await db.execute(
                select(Visit.id).where(
                    Visit.external_visit_order_no.in_(dzdh_set),
                    visit_scope_condition(scope),
                )
            )
        ).scalars().all()
    )

    items = _build_daily_visit_order_items(
        vos,
        visits,
        recording_id=recording_id,
        accessible_visit_ids=accessible_visit_ids,
    )

    return {
        "items": items,
        "recording_date": rec_date,
        "total": len(items),
        "scope_mode": normalized_scope_mode,
        "keyword": keyword or "",
    }


@router.get("/{visit_order_id}", response_model=VisitOrderOut)
async def get_visit_order(
    visit_order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    vo = (
        await db.execute(
            select(VisitOrder).where(VisitOrder.id == visit_order_id, visit_order_scope_condition(await build_permission_scope(current_user)))
        )
    ).scalar_one_or_none()
    if vo is None:
        raise HTTPException(status_code=404, detail="到诊单不存在")
    department_advisor_map = await _load_department_advisor_map(db, [vo])
    return _to_out(vo, department_advisor_map.get(_visit_order_sap_key(vo)))


@router.get("/{visit_order_id}/recording-match", response_model=VisitOrderRecordingMatchOut)
async def get_visit_order_recording_match(
    visit_order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    vo = (
        await db.execute(
            select(VisitOrder.id).where(VisitOrder.id == visit_order_id, visit_order_scope_condition(await build_permission_scope(current_user)))
        )
    ).scalar_one_or_none()
    if vo is None:
        raise HTTPException(status_code=404, detail="到诊单不存在")
    result = await analyze_visit_order_recording_match(db, visit_order_id)
    if result is None:
        raise HTTPException(status_code=404, detail="到诊单不存在")
    return result
