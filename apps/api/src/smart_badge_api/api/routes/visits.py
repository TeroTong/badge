from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, and_, cast, distinct, exists, false, func, or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.api.analysis_normalization import normalize_analysis_result
from smart_badge_api.api.archive_candidates import build_pending_archive_recordings_by_visit_id
from smart_badge_api.api.data_scope import (
    build_permission_scope,
    recording_scope_condition,
    visit_order_scope_condition,
    visit_scope_condition,
)
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.api.hospital_scope import normalize_hospital_code, visit_hospital_condition
from smart_badge_api.core.permissions import normalize_permission_role
from smart_badge_api.customer_type import (
    customer_type_from_visit_order,
    normalize_customer_type_code,
    normalize_customer_type_label,
)
from smart_badge_api.db.models import AnalysisTask, Customer, Device, Recording, RecordingVisitLink, Staff, Visit, VisitOrder
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.pagination import make_page_response
from smart_badge_api.schemas.visits import (
    CustomerVisitBatchOut,
    VisitCreate,
    VisitDateSummaryOut,
    VisitDetailOut,
    VisitDetailRecordingOut,
    VisitOrderContextOut,
    VisitOrderLineItemOut,
    VisitOut,
    VisitPageOut,
    VisitUpdate,
)
from smart_badge_api.visit_linking import ordered_visit_recording_links

router = APIRouter(prefix="/visits", tags=["接诊记录"])


async def _get_scoped_visit(db: AsyncSession, visit_id: str, current_user) -> Visit | None:
    scope = await build_permission_scope(current_user)
    return (
        await db.execute(
            select(Visit)
            .where(Visit.id == visit_id, visit_scope_condition(scope))
            .options(selectinload(Visit.customer), *_load_opts())
        )
    ).scalar_one_or_none()


def _recording_count_subquery(scope):
    return (
        select(RecordingVisitLink.visit_id, func.count(distinct(RecordingVisitLink.recording_id)).label("recording_count"))
        .join(Recording, Recording.id == RecordingVisitLink.recording_id)
        .where(recording_scope_condition(scope))
        .group_by(RecordingVisitLink.visit_id)
        .subquery()
    )


async def _resolve_visible_visit_ids_for_scope(
    db: AsyncSession,
    scope,
) -> list[str] | None:
    """Materialize visit IDs that satisfy `visit_scope_condition`.

    Works for any role with a staff_id (staff / hospital_admin / super_admin /
    system_admin). _resolve_managed_staff_ids_for_scope handles role-level
    filtering (super_admin gets all subordinates regardless of role level).

    Returns None only when there's no staff_id (caller falls back to EXISTS).
    """
    if normalize_permission_role(scope.role) in {"super_admin", "system_admin"}:
        return None
    if not scope.staff_id:
        return None
    managed_ids = await _resolve_managed_staff_ids_for_scope(db, scope)
    if not managed_ids:
        return []
    parts = [
        select(Visit.id.label("visit_id")).where(Visit.consultant_id.in_(managed_ids)),
        select(Visit.id.label("visit_id")).where(Visit.doctor_id.in_(managed_ids)),
        select(Visit.id.label("visit_id"))
        .join(Recording, Recording.visit_id == Visit.id)
        .where(Recording.staff_id.in_(managed_ids)),
        select(Visit.id.label("visit_id"))
        .join(RecordingVisitLink, RecordingVisitLink.visit_id == Visit.id)
        .join(Recording, Recording.id == RecordingVisitLink.recording_id)
        .where(Recording.staff_id.in_(managed_ids)),
    ]
    staff_meta = (
        await db.execute(
            select(Staff.external_account, Staff.hospital_code).where(
                Staff.id.in_(managed_ids),
                Staff.external_account.is_not(None),
                Staff.hospital_code.is_not(None),
            )
        )
    ).all()
    if staff_meta:
        from collections import defaultdict
        by_hosp: dict[str, list[str]] = defaultdict(list)
        for ext_acc, hosp in staff_meta:
            by_hosp[hosp].append(ext_acc)
        for hosp, ext_accs in by_hosp.items():
            parts.append(
                select(Visit.id.label("visit_id"))
                .join(VisitOrder, VisitOrder.dzdh == Visit.external_visit_order_no)
                .where(
                    Visit.external_visit_order_no.is_not(None),
                    VisitOrder.jgbm == hosp,
                    or_(
                        VisitOrder.fzuer.in_(ext_accs),
                        VisitOrder.d_fzuer.in_(ext_accs),
                        VisitOrder.fzr_id_dq.in_(ext_accs),
                        VisitOrder.advxc.in_(ext_accs),
                        VisitOrder.assxc.in_(ext_accs),
                        VisitOrder.advyq.in_(ext_accs),
                        VisitOrder.yyuer.in_(ext_accs),
                        VisitOrder.vipkf.in_(ext_accs),
                        VisitOrder.d_vipkf.in_(ext_accs),
                    ),
                )
            )
    rows = (await db.execute(union(*parts))).all()
    return [r[0] for r in rows if r[0]]


async def _resolve_managed_staff_ids_for_scope(db: AsyncSession, scope) -> list[str]:
    if normalize_permission_role(scope.role) in {"super_admin", "system_admin"}:
        return []
    if not scope.staff_id:
        return []
    if scope.role == "single_staff":
        return [scope.staff_id]
    from smart_badge_api.db.models import StaffManagementRelation
    from smart_badge_api.core.permissions import (
        PERMISSION_ROLE_LEVELS,
        LEGACY_STAFF_PERMISSION_ROLE_MAP,
    )
    role = normalize_permission_role(scope.role)
    actor_level = PERMISSION_ROLE_LEVELS.get(role, PERMISSION_ROLE_LEVELS["staff"])
    role_levels = {
        **PERMISSION_ROLE_LEVELS,
        **{
            legacy: PERMISSION_ROLE_LEVELS[normalized]
            for legacy, normalized in LEGACY_STAFF_PERMISSION_ROLE_MAP.items()
        },
    }
    rows = (
        await db.execute(
            select(StaffManagementRelation.subordinate_staff_id, Staff.permission_role)
            .join(Staff, Staff.id == StaffManagementRelation.subordinate_staff_id)
            .where(
                StaffManagementRelation.manager_staff_id == scope.staff_id,
                Staff.is_active.is_(True),
            )
        )
    ).all()
    ids: set[str] = {scope.staff_id}
    for sub_id, sub_role in rows:
        sub_level = role_levels.get(sub_role, PERMISSION_ROLE_LEVELS["staff"])
        if role == "super_admin" or sub_level <= actor_level:
            ids.add(sub_id)
    return list(ids)


def _visit_order_summary_subquery():
    return (
        select(
            VisitOrder.dzdh.label("dzdh"),
            func.max(VisitOrder.remark_dz).label("project_hint"),
            func.max(VisitOrder.kut30_dq).label("customer_type_code"),
            func.max(VisitOrder.kut30_dq_txt).label("customer_type_text"),
        )
        .group_by(VisitOrder.dzdh)
        .subquery()
    )


def _to_out(
    visit: Visit,
    *,
    customer_name: str,
    customer_code: str | None,
    customer_source: str | None,
    recording_count: int,
    doctor_name: str | None = None,
    customer_type_code: str | None = None,
    customer_type_label: str | None = None,
    arrival_purpose: str | None = None,
    project_needs: str | None = None,
) -> VisitOut:
    normalized_customer_type_code = normalize_customer_type_code(customer_type_code)
    normalized_customer_type_label = normalize_customer_type_label(normalized_customer_type_code, customer_type_label)
    return VisitOut(
        id=visit.id,
        customer_id=visit.customer_id,
        customer_name=customer_name,
        customer_code=customer_code,
        customer_source=customer_source,
        consultant_id=visit.consultant_id,
        consultant_name=visit.consultant.name if visit.consultant else None,
        doctor_id=visit.doctor_id,
        doctor_name=_first_non_empty(doctor_name, visit.doctor.name if visit.doctor else None),
        status=visit.status,
        deal_status=visit.deal_status,
        visit_date=visit.visit_date,
        visit_time=visit.visit_time,
        deposit_principal=float(visit.deposit_principal) if visit.deposit_principal is not None else None,
        deposit_bonus=float(visit.deposit_bonus) if visit.deposit_bonus is not None else None,
        recording_count=recording_count,
        customer_type_code=normalized_customer_type_code,
        customer_type_label=normalized_customer_type_label,
        arrival_purpose=_first_non_empty(arrival_purpose, visit.arrival_purpose),
        project_needs=_first_non_empty(project_needs, visit.project_needs),
        notes=visit.notes,
        created_at=visit.created_at.isoformat() if visit.created_at else "",
    )


def _load_opts():
    return [
        selectinload(Visit.consultant),
        selectinload(Visit.doctor),
    ]


def _build_excerpt(text: str | None, limit: int = 140) -> str | None:
    if not text:
        return None
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3].rstrip()}..."


def _visit_ordering():
    return (
        Visit.visit_date.desc().nulls_last(),
        Visit.visit_time.desc().nulls_last(),
        Visit.created_at.desc(),
    )


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _date_summary_key(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text[:10] if text else None


def _build_visit_date_summaries(rows: list[tuple[object, int]]) -> list[VisitDateSummaryOut]:
    summaries = [
        VisitDateSummaryOut(date=_date_summary_key(value), total=int(total or 0))
        for value, total in rows
    ]
    summaries.sort(key=lambda item: (item.date is not None, item.date or ""), reverse=True)
    return summaries


def _join_unique_values(values: list[Any], limit: int = 4, separator: str = "、") -> str | None:
    items: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in items:
            items.append(text)
    if not items:
        return None
    return separator.join(items[:limit])


def _build_visit_order_line_item(order: VisitOrder) -> VisitOrderLineItemOut:
    return VisitOrderLineItemOut(
        fzdh=_first_non_empty(order.fzdh),
        dzseg=_first_non_empty(order.dzseg),
        triage_staff_code=_first_non_empty(order.advxc, order.fzuer, order.fzr_id_dq),
        triage_staff_name=_first_non_empty(order.advxc_long, order.fzr_name_dq),
        triage_time=_first_non_empty(order.fzsj),
        consult_time=_first_non_empty(order.jzsj),
        triage_status_text=_first_non_empty(order.fzsta_txt),
        deal_status_text=_first_non_empty(order.jcsta_txt),
        consult_project=_first_non_empty(order.remark_dz),
        note_summary=_join_unique_values([order.remark_dz], limit=3, separator="；"),
    )


def _project_needs_from_visit_orders(visit_orders: list[VisitOrder]) -> str | None:
    return _first_non_empty(*[order.remark_dz for order in visit_orders])


def _extract_recording_id(file_name: str) -> str | None:
    if file_name.startswith("recording_") and file_name.endswith(".json"):
        return file_name.removeprefix("recording_").removesuffix(".json")
    return None


def _extract_text_content(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        for key in ("content", "value", "label", "text"):
            nested = value.get(key)
            text = _extract_text_content(nested)
            if text:
                return text
    return None


def _find_labeled_value(payload: Any, label: str) -> Any:
    if isinstance(payload, dict):
        if label in payload:
            return payload[label]
        for value in payload.values():
            found = _find_labeled_value(value, label)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_labeled_value(item, label)
            if found is not None:
                return found
    return None


def _extract_project_needs(result: dict[str, Any] | None) -> list[str]:
    if not isinstance(result, dict):
        return []

    target = _find_labeled_value(result, "项目需求")
    content = target.get("content") if isinstance(target, dict) else target
    items: list[str] = []

    if isinstance(content, list):
        for item in content:
            text = _extract_text_content(item)
            if text and text not in items:
                items.append(text)
    else:
        text = _extract_text_content(content)
        if text:
            items.append(text)

    if items:
        return items[:3]

    profile = result.get("customer_profile") if isinstance(result.get("customer_profile"), dict) else {}
    tags = profile.get("tags") if isinstance(profile, dict) else []
    if isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            category = str(tag.get("category") or "")
            if "客户求美需求" not in category:
                continue
            value = _extract_text_content(tag.get("value"))
            if value and value not in items:
                items.append(value)
    return items[:3]


def _filter_recordings_for_scope(recordings: list[Recording], scope) -> list[Recording]:
    if normalize_permission_role(scope.role) == "staff" and scope.staff_id:
        return [recording for recording in recordings if recording.staff_id == scope.staff_id]
    return recordings


def _sort_recordings(recordings: list[Recording]) -> list[Recording]:
    return sorted(
        recordings,
        key=lambda recording: (
            recording.created_at.isoformat() if recording.created_at else "",
            recording.id,
        ),
        reverse=True,
    )


def _visible_visit_recordings(visit: Visit, scope) -> list[Recording]:
    merged: dict[str, Recording] = {}

    for link in ordered_visit_recording_links(visit):
        recording = link.recording
        if recording is None or recording.id in merged:
            continue
        merged[recording.id] = recording

    for recording in _sort_recordings(list(visit.recordings or [])):
        if recording.id in merged:
            continue
        merged[recording.id] = recording

    return _filter_recordings_for_scope(list(merged.values()), scope)



def _extract_visit_card_snapshot(visit: Visit, result: dict[str, Any] | None) -> dict[str, Any]:
    _ = normalize_analysis_result(result) if result else None
    return {
        "arrival_purpose": visit.arrival_purpose,
    }


def _build_visit_order_context(visit_orders: list[VisitOrder]) -> VisitOrderContextOut | None:
    if not visit_orders:
        return None

    sorted_orders = sorted(visit_orders, key=lambda order: (order.dzseg or "", order.fzdh or "", order.id or ""))
    primary = sorted_orders[0]
    customer_type_code, customer_type_label = customer_type_from_visit_order(primary)

    return VisitOrderContextOut(
        jgbm=_first_non_empty(primary.jgbm),
        customer_type_code=customer_type_code,
        customer_type_label=customer_type_label,
        triage_time=_first_non_empty(primary.fzsj),
        consult_time=_first_non_empty(primary.jzsj),
        arrival_status=_first_non_empty(primary.dzsta_txt, primary.fzsta_txt, primary.dztyp_txt),
        deal_status_text=_first_non_empty(primary.jcsta_txt),
        visit_purpose=_first_non_empty(primary.dymd_txt),
        consult_project=_join_unique_values([order.remark_dz for order in visit_orders]),
        demand_remark=_first_non_empty(primary.remark_dz),
        line_items=[_build_visit_order_line_item(order) for order in sorted_orders],
    )


async def _build_visit_card_snapshot_map(db: AsyncSession, visits: list[Visit], scope) -> dict[str, dict[str, Any]]:
    visit_ids = [visit.id for visit in visits]
    if not visit_ids:
        return {}

    recording_rows = (
        await db.execute(
            select(RecordingVisitLink.visit_id, RecordingVisitLink.recording_id)
            .join(Recording, Recording.id == RecordingVisitLink.recording_id)
            .where(
                RecordingVisitLink.visit_id.in_(visit_ids),
                recording_scope_condition(scope),
            )
        )
    ).all()
    if not recording_rows:
        return {}

    file_name_to_visit_ids: dict[str, list[str]] = {}
    for visit_id, recording_id in recording_rows:
        file_name_to_visit_ids.setdefault(f"recording_{recording_id}.json", []).append(visit_id)

    tasks = (
        await db.execute(
            select(AnalysisTask)
            .where(AnalysisTask.file_name.in_(list(file_name_to_visit_ids.keys())))
            .order_by(AnalysisTask.created_at.desc())
        )
    ).scalars().all()

    latest_task_by_visit_id: dict[str, AnalysisTask] = {}
    for task in tasks:
        for visit_id in file_name_to_visit_ids.get(task.file_name, []):
            latest_task_by_visit_id.setdefault(visit_id, task)

    visit_by_id = {visit.id: visit for visit in visits}
    snapshot_map: dict[str, dict[str, Any]] = {}
    for visit_id, task in latest_task_by_visit_id.items():
        visit = visit_by_id.get(visit_id)
        if visit is None:
            continue
        snapshot_map[visit_id] = _extract_visit_card_snapshot(visit, task.result)
    return snapshot_map


@router.get("", response_model=VisitPageOut)
async def list_visits(
    customer_id: str | None = Query(None),
    status: str | None = Query(None),
    has_recharge: bool | None = Query(None),
    keyword: str | None = Query(None),
    consultant_id: str | None = Query(None),
    participant_staff_id: str | None = Query(None),
    source: str | None = Query(None),
    hospital_code: str | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    has_recordings: bool | None = Query(None),
    include_date_summaries: bool = Query(False),
    fast_page: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    customer_id = customer_id if isinstance(customer_id, str) else None
    status = status if isinstance(status, str) else None
    keyword = keyword if isinstance(keyword, str) else None
    consultant_id = consultant_id if isinstance(consultant_id, str) else None
    participant_staff_id = participant_staff_id if isinstance(participant_staff_id, str) else None
    source = source if isinstance(source, str) else None
    requested_hospital_code = normalize_hospital_code(hospital_code)
    scope = await build_permission_scope(current_user)
    visible_visit_ids = await _resolve_visible_visit_ids_for_scope(db, scope)
    recording_count_sub = _recording_count_subquery(scope)
    visit_order_summary_sub = _visit_order_summary_subquery()
    recording_count = func.coalesce(recording_count_sub.c.recording_count, 0)

    if visible_visit_ids is None:
        scope_clause = visit_scope_condition(scope)
    elif visible_visit_ids:
        scope_clause = Visit.id.in_(visible_visit_ids)
    else:
        scope_clause = false()

    stmt = (
        select(
            Visit,
            Customer.name.label("customer_name"),
            Customer.external_customer_code.label("customer_code"),
            Customer.source.label("customer_source"),
            recording_count.label("recording_count"),
            visit_order_summary_sub.c.project_hint.label("visit_order_project_hint"),
            visit_order_summary_sub.c.customer_type_code.label("customer_type_code"),
            visit_order_summary_sub.c.customer_type_text.label("customer_type_text"),
        )
        .join(Customer, Visit.customer_id == Customer.id)
        .outerjoin(recording_count_sub, Visit.id == recording_count_sub.c.visit_id)
        .outerjoin(visit_order_summary_sub, Visit.external_visit_order_no == visit_order_summary_sub.c.dzdh)
        .where(scope_clause)
        .order_by(*_visit_ordering())
        .options(*_load_opts())
    )

    if customer_id:
        stmt = stmt.where(Visit.customer_id == customer_id)
    if status:
        stmt = stmt.where(Visit.status == status)
    if has_recharge is True:
        stmt = stmt.where(func.coalesce(Visit.deposit_principal, 0) > 0)
    if has_recharge is False:
        stmt = stmt.where(func.coalesce(Visit.deposit_principal, 0) <= 0)
    if consultant_id:
        stmt = stmt.where(Visit.consultant_id == consultant_id)
    if participant_staff_id:
        stmt = stmt.where(
            or_(
                Visit.consultant_id == participant_staff_id,
                Visit.doctor_id == participant_staff_id,
                exists(
                    select(RecordingVisitLink.id)
                    .join(Recording, Recording.id == RecordingVisitLink.recording_id)
                    .where(
                        RecordingVisitLink.visit_id == Visit.id,
                        Recording.staff_id == participant_staff_id,
                    )
                ),
            )
        )
    if source:
        stmt = stmt.where(Customer.source == source)
    if requested_hospital_code:
        stmt = stmt.where(visit_hospital_condition(requested_hospital_code))
    if date_from:
        stmt = stmt.where(Visit.visit_date >= date_from)
    if date_to:
        stmt = stmt.where(Visit.visit_date <= date_to)
    if has_recordings is True:
        stmt = stmt.where(recording_count > 0)
    if has_recordings is False:
        stmt = stmt.where(recording_count == 0)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                Customer.name.ilike(like),
                Customer.external_customer_code.ilike(like),
                cast(Visit.id, String).ilike(like),
            )
        )

    if fast_page and not include_date_summaries:
        rows_with_probe = (
            await db.execute(stmt.offset((page - 1) * page_size).limit(page_size + 1))
        ).all()
        has_more = len(rows_with_probe) > page_size
        rows = rows_with_probe[:page_size]
        total = page * page_size + 1 if has_more else (page - 1) * page_size + len(rows)
        date_summaries = []
    else:
        filtered_subquery = stmt.order_by(None).subquery()
        total: int = (await db.execute(select(func.count()).select_from(filtered_subquery))).scalar_one()
        if include_date_summaries:
            date_summary_rows = (
                await db.execute(
                    select(filtered_subquery.c.visit_date, func.count())
                    .group_by(filtered_subquery.c.visit_date)
                )
            ).all()
            date_summaries = _build_visit_date_summaries(date_summary_rows)
        else:
            date_summaries = []
        rows = (await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))).all()
    visits = [visit for visit, *_ in rows]
    # NOTE: previously called `_build_visit_card_snapshot_map` here, which loaded
    # all RecordingVisitLink + AnalysisTask rows for the page just to surface
    # `arrival_purpose` — a value already present on `Visit`. Skipping it shaves
    # several seconds off the request and produces identical output (see _to_out).
    snapshot_map: dict[str, dict[str, Any]] = {}

    page_response = make_page_response(
        [
            _to_out(
                visit,
                customer_name=customer_name,
                customer_code=customer_code,
                customer_source=customer_source,
                recording_count=int(visit_recording_count or 0),
                customer_type_code=customer_type_code,
                customer_type_label=customer_type_text,
                project_needs=visit_order_project_hint,
                **snapshot_map.get(visit.id, {}),
            )
            for (
                visit,
                customer_name,
                customer_code,
                customer_source,
                visit_recording_count,
                visit_order_project_hint,
                customer_type_code,
                customer_type_text,
            ) in rows
        ],
        total,
        page,
        page_size,
    )
    return VisitPageOut(
        items=page_response.items,
        total=page_response.total,
        page=page_response.page,
        page_size=page_response.page_size,
        pages=page_response.pages,
        date_summaries=date_summaries,
    )


@router.get("/by-customers", response_model=list[CustomerVisitBatchOut])
async def list_visits_by_customers(
    customer_id: list[str] = Query(..., description="客户ID，可重复传参"),
    per_customer_limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    customer_ids = list(dict.fromkeys(item.strip() for item in customer_id if item.strip()))
    if not customer_ids:
        return []

    scope = await build_permission_scope(current_user)
    recording_count_sub = _recording_count_subquery(scope)
    visit_order_summary_sub = _visit_order_summary_subquery()
    recording_count = func.coalesce(recording_count_sub.c.recording_count, 0)

    ranked_visits = (
        select(
            Visit.id.label("visit_id"),
            func.row_number()
            .over(partition_by=Visit.customer_id, order_by=_visit_ordering())
            .label("visit_rank"),
        )
        .where(
            visit_scope_condition(scope),
            Visit.customer_id.in_(customer_ids),
        )
        .subquery()
    )

    stmt = (
        select(
            Visit,
            Customer.name.label("customer_name"),
            Customer.external_customer_code.label("customer_code"),
            Customer.source.label("customer_source"),
            recording_count.label("recording_count"),
            visit_order_summary_sub.c.project_hint.label("visit_order_project_hint"),
            visit_order_summary_sub.c.customer_type_code.label("customer_type_code"),
            visit_order_summary_sub.c.customer_type_text.label("customer_type_text"),
        )
        .join(ranked_visits, ranked_visits.c.visit_id == Visit.id)
        .join(Customer, Visit.customer_id == Customer.id)
        .outerjoin(recording_count_sub, Visit.id == recording_count_sub.c.visit_id)
        .outerjoin(visit_order_summary_sub, Visit.external_visit_order_no == visit_order_summary_sub.c.dzdh)
        .where(
            ranked_visits.c.visit_rank <= per_customer_limit,
        )
        .order_by(Visit.customer_id.asc(), *_visit_ordering())
        .options(*_load_opts())
    )

    rows = (await db.execute(stmt)).all()
    grouped_rows: dict[str, list[tuple]] = {item: [] for item in customer_ids}

    for row in rows:
        visit = row[0]
        bucket = grouped_rows.setdefault(visit.customer_id, [])
        if len(bucket) >= per_customer_limit:
            continue
        bucket.append(row)

    # Customer list cards only need timeline summaries; detailed analysis snapshots
    # are loaded by the customer/visit detail pages.
    snapshot_map: dict[str, dict[str, Any]] = {}

    return [
        CustomerVisitBatchOut(
            customer_id=item,
            visits=[
                _to_out(
                    visit,
                    customer_name=customer_name,
                    customer_code=customer_code,
                    customer_source=customer_source,
                    recording_count=int(visit_recording_count or 0),
                    customer_type_code=customer_type_code,
                    customer_type_label=customer_type_text,
                    project_needs=visit_order_project_hint,
                    **snapshot_map.get(visit.id, {}),
                )
                for (
                    visit,
                    customer_name,
                    customer_code,
                    customer_source,
                    visit_recording_count,
                    visit_order_project_hint,
                    customer_type_code,
                    customer_type_text,
                ) in grouped_rows.get(item, [])
            ],
        )
        for item in customer_ids
    ]


@router.get("/{visit_id}/detail", response_model=VisitDetailOut)
async def get_visit_detail(
    visit_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    scope = await build_permission_scope(current_user)
    visit = (
        await db.execute(
            select(Visit)
            .where(Visit.id == visit_id, visit_scope_condition(scope))
            .options(
                selectinload(Visit.customer),
                *_load_opts(),
                selectinload(Visit.recordings).selectinload(Recording.transcript),
                selectinload(Visit.recordings).selectinload(Recording.staff),
                selectinload(Visit.recording_links).selectinload(RecordingVisitLink.recording).selectinload(Recording.transcript),
                selectinload(Visit.recording_links).selectinload(RecordingVisitLink.recording).selectinload(Recording.staff),
            )
        )
    ).scalar_one_or_none()
    if not visit:
        raise HTTPException(404, "鎺ヨ瘖璁板綍涓嶅瓨鍦?")

    visit_orders: list[VisitOrder] = []
    if visit.external_visit_order_no:
        visit_orders = (
            await db.execute(
                select(VisitOrder)
                .where(
                    VisitOrder.dzdh == visit.external_visit_order_no,
                    visit_order_scope_condition(scope),
                )
                .order_by(VisitOrder.dzseg.asc())
            )
        ).scalars().all()

    ordered_links = ordered_visit_recording_links(visit)
    ordered_recordings = _visible_visit_recordings(visit, scope)
    link_by_recording_id = {link.recording_id: link for link in ordered_links}
    device_ids = {recording.device_id for recording in ordered_recordings if recording.device_id}
    device_code_map: dict[str, str] = {}
    if device_ids:
        device_code_rows = await db.execute(select(Device.id, Device.device_code).where(Device.id.in_(device_ids)))
        device_code_map = {device_id: device_code for device_id, device_code in device_code_rows.all()}
    file_name_map = {f"recording_{recording.id}.json": recording.id for recording in ordered_recordings}
    task_by_recording_id: dict[str, AnalysisTask] = {}
    if file_name_map:
        tasks = (
            await db.execute(
                select(AnalysisTask)
                .where(AnalysisTask.file_name.in_(list(file_name_map.keys())))
                .order_by(AnalysisTask.created_at.desc())
            )
        ).scalars().all()
        for task in tasks:
            recording_id = file_name_map.get(task.file_name)
            if recording_id and recording_id not in task_by_recording_id:
                task_by_recording_id[recording_id] = task

    recording_items: list[VisitDetailRecordingOut] = []
    transcript_count = 0
    analyzed_recording_count = 0
    pending_archive_recordings_by_visit_id = await build_pending_archive_recordings_by_visit_id(db, [visit], scope)

    for recording in ordered_recordings:
        transcript = recording.transcript
        task = task_by_recording_id.get(recording.id)
        if transcript and transcript.status == "completed":
            transcript_count += 1
        if task and task.status == "done":
            analyzed_recording_count += 1

        recording_items.append(
            VisitDetailRecordingOut(
                id=recording.id,
                file_name=recording.file_name,
                is_primary=bool(link_by_recording_id.get(recording.id).is_primary) if link_by_recording_id.get(recording.id) else recording.visit_id == visit.id,
                device_id=recording.device_id,
                device_code=device_code_map.get(recording.device_id or ""),
                staff_name=recording.staff.name if recording.staff else None,
                staff_badge_id=recording.staff.badge_id if recording.staff else None,
                status=recording.status,
                duration_seconds=recording.duration_seconds,
                created_at=recording.created_at.isoformat() if recording.created_at else "",
                transcript_id=transcript.id if transcript else None,
                transcript_status=transcript.status if transcript else None,
                transcript_provider=transcript.asr_provider if transcript else None,
                transcript_excerpt=_build_excerpt(transcript.full_text if transcript else None),
                analysis_task_id=task.id if task else None,
                analysis_status=task.status if task else None,
                analysis_overall_score=float(task.overall_score) if task and task.overall_score is not None else None,
                analysis_completed_at=task.completed_at.isoformat() if task and task.completed_at else None,
                analysis_result=normalize_analysis_result(task.result) if task else None,
            )
        )

    primary_recording = (
        next((item for item in recording_items if item.analysis_result), None)
        or next((item for item in recording_items if item.analysis_status), None)
        or next((item for item in recording_items if item.transcript_excerpt), None)
        or (recording_items[0] if recording_items else None)
    )

    customer_type_code, customer_type_label = (
        customer_type_from_visit_order(visit_orders[0]) if visit_orders else (None, None)
    )
    base = _to_out(
        visit,
        customer_name=visit.customer.name if visit.customer else "",
        customer_code=visit.customer.external_customer_code if visit.customer else None,
        customer_source=visit.customer.source if visit.customer else None,
        recording_count=len(recording_items),
        customer_type_code=customer_type_code,
        customer_type_label=customer_type_label,
        project_needs=_project_needs_from_visit_orders(visit_orders),
        **(_extract_visit_card_snapshot(visit, primary_recording.analysis_result if primary_recording else None)),
    )

    return VisitDetailOut(
        **base.model_dump(),
        customer_gender=visit.customer.gender if visit.customer else None,
        customer_age=visit.customer.age if visit.customer else None,
        customer_wechat_external_uid=visit.customer.wechat_external_uid if visit.customer else None,
        customer_notes=visit.customer.notes if visit.customer else None,
        transcript_count=transcript_count,
        analyzed_recording_count=analyzed_recording_count,
        latest_recording_id=primary_recording.id if primary_recording else None,
        latest_transcript_id=primary_recording.transcript_id if primary_recording else None,
        latest_analysis_task_id=primary_recording.analysis_task_id if primary_recording else None,
        latest_analysis_status=primary_recording.analysis_status if primary_recording else None,
        latest_analysis_overall_score=primary_recording.analysis_overall_score if primary_recording else None,
        latest_analysis_completed_at=primary_recording.analysis_completed_at if primary_recording else None,
        latest_analysis_result=primary_recording.analysis_result if primary_recording else None,
        latest_transcript_excerpt=primary_recording.transcript_excerpt if primary_recording else None,
        visit_order_context=_build_visit_order_context(visit_orders),
        recordings=recording_items,
        pending_archive_recordings=pending_archive_recordings_by_visit_id.get(visit.id, []),
    )


@router.get("/{visit_id}", response_model=VisitOut)
async def get_visit(
    visit_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    visit = await _get_scoped_visit(db, visit_id, current_user)
    if not visit:
        raise HTTPException(404, "接诊记录不存在")

    scope = await build_permission_scope(current_user)
    recording_count = (
        await db.execute(
            select(func.count(distinct(RecordingVisitLink.recording_id)))
            .join(Recording, Recording.id == RecordingVisitLink.recording_id)
            .where(
                RecordingVisitLink.visit_id == visit_id,
                recording_scope_condition(scope),
            )
        )
    ).scalar_one()
    visit_order_project_needs = None
    if visit.external_visit_order_no:
        visit_order_row = (
            await db.execute(
                select(
                    func.max(VisitOrder.remark_dz).label("project_hint"),
                    func.max(VisitOrder.kut30_dq).label("customer_type_code"),
                    func.max(VisitOrder.kut30_dq_txt).label("customer_type_text"),
                ).where(
                    VisitOrder.dzdh == visit.external_visit_order_no,
                    visit_order_scope_condition(scope),
                )
            )
        ).one()
        visit_order_project_needs = visit_order_row.project_hint
        customer_type_code = visit_order_row.customer_type_code
        customer_type_text = visit_order_row.customer_type_text
    else:
        customer_type_code = None
        customer_type_text = None
    return _to_out(
        visit,
        customer_name=visit.customer.name if visit.customer else "",
        customer_code=visit.customer.external_customer_code if visit.customer else None,
        customer_source=visit.customer.source if visit.customer else None,
        recording_count=int(recording_count or 0),
        customer_type_code=customer_type_code,
        customer_type_label=customer_type_text,
        project_needs=visit_order_project_needs,
        **(await _build_visit_card_snapshot_map(db, [visit], scope)).get(visit.id, {}),
    )


@router.post("", response_model=VisitOut, status_code=201)
async def create_visit(
    body: VisitCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    customer = await db.get(Customer, body.customer_id)
    if not customer:
        raise HTTPException(400, "客户不存在")
    if body.consultant_id and not await db.get(Staff, body.consultant_id):
        raise HTTPException(400, "咨询师不存在")
    if body.doctor_id and not await db.get(Staff, body.doctor_id):
        raise HTTPException(400, "医生不存在")

    visit = Visit(**body.model_dump())
    db.add(visit)
    await db.commit()

    stored = (
        await db.execute(
            select(Visit)
            .where(Visit.id == visit.id)
            .options(selectinload(Visit.customer), *_load_opts())
        )
    ).scalar_one()
    return _to_out(
        stored,
        customer_name=stored.customer.name if stored.customer else "",
        customer_code=stored.customer.external_customer_code if stored.customer else None,
        customer_source=stored.customer.source if stored.customer else None,
        recording_count=0,
    )


@router.put("/{visit_id}", response_model=VisitOut)
async def update_visit(
    visit_id: str,
    body: VisitUpdate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    visit = await _get_scoped_visit(db, visit_id, current_user)
    if not visit:
        raise HTTPException(404, "接诊记录不存在")

    updates = body.model_dump(exclude_unset=True)
    if "consultant_id" in updates and updates["consultant_id"] and not await db.get(Staff, updates["consultant_id"]):
        raise HTTPException(400, "咨询师不存在")
    if "doctor_id" in updates and updates["doctor_id"] and not await db.get(Staff, updates["doctor_id"]):
        raise HTTPException(400, "医生不存在")

    for key, value in updates.items():
        setattr(visit, key, value)
    await db.commit()

    stored = (
        await db.execute(
            select(Visit)
            .where(Visit.id == visit_id)
            .options(selectinload(Visit.customer), *_load_opts())
        )
    ).scalar_one()
    recording_count = (
        await db.execute(select(func.count(distinct(RecordingVisitLink.recording_id))).where(RecordingVisitLink.visit_id == visit_id))
    ).scalar_one()
    return _to_out(
        stored,
        customer_name=stored.customer.name if stored.customer else "",
        customer_code=stored.customer.external_customer_code if stored.customer else None,
        customer_source=stored.customer.source if stored.customer else None,
        recording_count=int(recording_count or 0),
        **(await _build_visit_card_snapshot_map(db, [stored])).get(stored.id, {}),
    )


@router.delete("/{visit_id}", status_code=204)
async def delete_visit(
    visit_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    visit = await _get_scoped_visit(db, visit_id, current_user)
    if not visit:
        raise HTTPException(404, "接诊记录不存在")
    await db.delete(visit)
    await db.commit()
