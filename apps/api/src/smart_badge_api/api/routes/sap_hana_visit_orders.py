from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import exists, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.data_scope import build_permission_scope
from smart_badge_api.api.deps import get_current_user, get_db
from smart_badge_api.core.permissions import is_global_role
from smart_badge_api.db.models import SapHanaVisitOrder, Staff, User
from smart_badge_api.schemas.pagination import PaginatedResponse, make_page_response
from smart_badge_api.schemas.sap_hana_visit_orders import (
    SapHanaVisitOrderDetailOut,
    SapHanaVisitOrderListOut,
)

router = APIRouter(prefix="/sap-hana-visit-orders", tags=["sap-hana-visit-orders"])


def _normalize_time(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return ""
    if len(digits) > 6:
        digits = digits[-6:]
    return digits.zfill(6)


def _pick_latest_triage_snapshot(items: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not items:
        return None

    def sort_key(entry_with_index: tuple[int, dict[str, Any]]) -> tuple[int, str, int]:
        index, entry = entry_with_index
        normalized_time = _normalize_time(entry.get("FZSJ"))
        return (1 if normalized_time else 0, normalized_time, index)

    return max(enumerate(items), key=sort_key)[1]


def _to_iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _to_list_out(row: SapHanaVisitOrder) -> SapHanaVisitOrderListOut:
    fzdata = row.fzdata or []
    latest = _pick_latest_triage_snapshot(fzdata)
    return SapHanaVisitOrderListOut(
        id=row.id,
        jgbm=row.jgbm,
        dzdh=row.dzdh,
        yydh=row.yydh,
        crtdt=row.crtdt,
        crttm=row.crttm,
        dzsta=row.dzsta,
        kunr=row.kunr,
        ninam=row.ninam,
        kusex=row.kusex,
        kulvl_dq=row.kulvl_dq,
        dzly=row.dzly,
        dymd=row.dymd,
        dztyp=row.dztyp,
        remark_dz=row.remark_dz,
        jgks=row.jgks,
        fzuer=row.fzuer,
        fzuer_long=row.fzuer_long,
        advyq=row.advyq,
        yyuer=row.yyuer,
        bhkx=row.bhkx,
        fzdata_count=len(fzdata),
        latest_fzdh=str(latest.get("FZDH") or "").strip() or None if latest else None,
        latest_advxc=str(latest.get("ADVXC") or "").strip() or None if latest else None,
        latest_advxc_long=str(latest.get("ADVXC_LONG") or "").strip() or None if latest else None,
        latest_fzsj=_normalize_time(latest.get("FZSJ")) or None if latest else None,
        latest_fzsta=str(latest.get("FZSTA") or "").strip() or None if latest else None,
        latest_jcsta=str(latest.get("JCSTA") or "").strip() or None if latest else None,
        last_received_at=_to_iso(row.last_received_at),
        created_at=_to_iso(row.created_at),
        updated_at=_to_iso(row.updated_at),
    )


def _to_detail_out(row: SapHanaVisitOrder) -> SapHanaVisitOrderDetailOut:
    base = _to_list_out(row).model_dump()
    return SapHanaVisitOrderDetailOut(
        **base,
        vipkf=row.vipkf,
        d_fzuer=row.d_fzuer,
        d_vipkf=row.d_vipkf,
        kusrc=row.kusrc,
        kusrc2=row.kusrc2,
        bjzx=row.bjzx,
        fzdata=row.fzdata or [],
        source_payload=row.source_payload or {},
    )


def sap_hana_visit_order_scope_condition(*, current_user: User, scope) -> Any:
    if is_global_role(scope.role):
        return true()

    if scope.role == "hospital_admin":
        return SapHanaVisitOrder.jgbm == scope.hospital_code if scope.hospital_code else false()

    staff_code_filters = [
        SapHanaVisitOrder.fzuer,
        SapHanaVisitOrder.d_fzuer,
        SapHanaVisitOrder.advyq,
        SapHanaVisitOrder.yyuer,
    ]
    if scope.staff_id:
        return exists(
            select(Staff.id).where(
                Staff.id == current_user.staff_id,
                Staff.external_account.is_not(None),
                or_(*[Staff.external_account == code for code in staff_code_filters]),
            )
        )

    return false()


@router.get("", response_model=PaginatedResponse[SapHanaVisitOrderListOut])
async def list_sap_hana_visit_orders(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str | None = Query(None),
    jgbm: str | None = Query(None),
    crtdt_start: str | None = Query(None),
    crtdt_end: str | None = Query(None),
):
    scope = await build_permission_scope(current_user)
    stmt = select(SapHanaVisitOrder).where(
        sap_hana_visit_order_scope_condition(current_user=current_user, scope=scope)
    )

    if keyword:
        normalized = keyword.strip()
        if normalized:
            pattern = f"%{normalized}%"
            stmt = stmt.where(
                or_(
                    SapHanaVisitOrder.dzdh.ilike(pattern),
                    SapHanaVisitOrder.ninam.ilike(pattern),
                    SapHanaVisitOrder.kunr.ilike(pattern),
                    SapHanaVisitOrder.yydh.ilike(pattern),
                )
            )

    if jgbm:
        stmt = stmt.where(SapHanaVisitOrder.jgbm == jgbm.strip())
    if crtdt_start:
        stmt = stmt.where(SapHanaVisitOrder.crtdt >= crtdt_start.strip())
    if crtdt_end:
        stmt = stmt.where(SapHanaVisitOrder.crtdt <= crtdt_end.strip())

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(SapHanaVisitOrder.updated_at.desc(), SapHanaVisitOrder.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return make_page_response([_to_list_out(row) for row in rows], total, page, page_size)


@router.get("/{item_id}", response_model=SapHanaVisitOrderDetailOut)
async def get_sap_hana_visit_order_detail(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    scope = await build_permission_scope(current_user)
    stmt = select(SapHanaVisitOrder).where(
        SapHanaVisitOrder.id == item_id,
        sap_hana_visit_order_scope_condition(current_user=current_user, scope=scope),
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="未找到对应的 SAP HANA 推送单据")
    return _to_detail_out(row)
