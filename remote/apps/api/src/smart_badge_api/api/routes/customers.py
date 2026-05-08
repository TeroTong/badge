from datetime import date, datetime, time, timedelta, timezone
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import distinct, exists, false, func, or_, select, true, union, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.api.analysis_normalization import normalize_analysis_result, normalize_profile_themes
from smart_badge_api.api.archive_candidates import build_pending_archive_recordings_by_visit_id
from smart_badge_api.api.data_scope import (
    build_permission_scope,
    customer_scope_condition,
    recording_scope_condition,
    visit_order_scope_condition,
    visit_scope_condition,
)
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.analysis.consultation_evaluation import normalize_consultation_dimension_name
from smart_badge_api.core.permissions import normalize_permission_role
from smart_badge_api.db.default_data import ensure_tag_categories
from smart_badge_api.db.models import AnalysisTask, Customer, Recording, RecordingVisitLink, SapPushLog, TagCategory, Visit, VisitOrder
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.customers import (
    CustomerCreate,
    CustomerDateSummaryOut,
    CustomerDetailOut,
    CustomerDetailRecordingEvalDimensionOut,
    CustomerDetailRecordingOut,
    CustomerDetailVisitOrderLineItemOut,
    CustomerDetailVisitOrderSummaryOut,
    CustomerDetailVisitOut,
    CustomerMergedAnalysisOut,
    CustomerMergedDimensionOut,
    CustomerMergedThemeOut,
    CustomerMergedTimelineOut,
    CustomerOut,
    CustomerPageOut,
    CustomerUpdate,
    CustomerVisitOrdersOut,
    TagCompletionOut,
    TagExtractionItem,
    VisitOrderGroupOut,
    VisitOrderItemOut,
)
from smart_badge_api.schemas.pagination import make_page_response
from smart_badge_api.sap_consultation import build_consultation_text
from smart_badge_api.tag_catalog_reference import (
    BIRTHDATE_TAG_CATEGORY,
    NEGATIVE_PROJECT_EMPTY_VALUE,
    NEGATIVE_PROJECT_TAG_CATEGORY,
    canonicalize_profile_tag_value,
    is_valid_profile_tag_value,
)
from smart_badge_api.visit_linking import ordered_visit_recording_links

router = APIRouter(prefix="/customers", tags=["客户档案"])
BEIJING_TZ = timezone(timedelta(hours=8))
_PROFILE_ANALYSIS_EXCLUDED_CATEGORY_NAMES = frozenset({"本次消费预算"})
_CUSTOMER_ARCHIVE_TAG_EVIDENCE = "已从客户档案同步"
_LEADING_SCORE_SUMMARY_RE = re.compile(
    r"^(?:(?:(?:六维(?:得分|总分)\s*\d+(?:\.\d+)?\s*\/\s*\d+(?:\.\d+)?|九点评价\s*\d+(?:\.\d+)?\s*\/\s*10(?:\.\d+)?))[。；\s]*)+"
)
_EVAL_DIMENSION_ORDER = {
    "医美专业知识": 0,
    "适应症获取": 1,
    "顾客标签获取": 2,
    "医院和医生介绍": 3,
    "老带新等特别事项": 4,
    "负面交流检测": 5,
}
_CUSTOMER_TYPE_LABELS = {
    "Q": "新客",
    "V": "老客",
}


def _should_ignore_tag_completion_value(category: str, value: str) -> bool:
    if category == NEGATIVE_PROJECT_TAG_CATEGORY and value == NEGATIVE_PROJECT_EMPTY_VALUE:
        return False
    if value in ("未明确", "未提及", "未知", "无", "N/A", "-"):
        return True
    return not is_valid_profile_tag_value(category, value)


async def _resolve_customer_archive_birthdate(
    db: AsyncSession,
    customer: Customer,
    scope,
) -> str | None:
    birthdays = (
        await db.execute(
            select(VisitOrder.customer_birthday)
            .join(Visit, Visit.external_visit_order_no == VisitOrder.dzdh)
            .where(
                Visit.customer_id == customer.id,
                visit_scope_condition(scope),
                VisitOrder.customer_birthday.is_not(None),
                VisitOrder.customer_birthday != "",
            )
            .order_by(
                Visit.visit_date.desc().nullslast(),
                Visit.created_at.desc().nullslast(),
                VisitOrder.crtdt.desc().nullslast(),
                VisitOrder.crttm.desc().nullslast(),
            )
        )
    ).scalars().all()
    for raw_value in birthdays:
        normalized = canonicalize_profile_tag_value(BIRTHDATE_TAG_CATEGORY, raw_value)
        if normalized and normalized != "-":
            return normalized
    return None


def _normalize_customer_type_code(value: object) -> str | None:
    text = str(value or "").strip().upper()
    return text if text in _CUSTOMER_TYPE_LABELS else None


def _normalize_customer_type_label(code: object, text: object = None) -> str | None:
    normalized_code = _normalize_customer_type_code(code)
    if normalized_code:
        return _CUSTOMER_TYPE_LABELS[normalized_code]

    normalized_text = str(text or "").strip()
    if not normalized_text:
        return None
    if "老客" in normalized_text or "会员" in normalized_text:
        return "老客"
    if "新客" in normalized_text or "潜客" in normalized_text:
        return "新客"
    return normalized_text


def _customer_type_sort_key(
    visit_date: date | None,
    visit_created_at: datetime | None,
    order_sjrq: str | None,
    order_crtdt: str | None,
    order_crttm: str | None,
) -> tuple[str, str, str]:
    date_text = (
        visit_date.isoformat()
        if isinstance(visit_date, date)
        else str(order_sjrq or order_crtdt or "").strip()
    )
    created_text = visit_created_at.isoformat() if isinstance(visit_created_at, datetime) else ""
    time_text = str(order_crttm or "").strip()
    return date_text, created_text, time_text


async def _customer_type_map(
    db: AsyncSession,
    customer_ids: list[str],
    scope,
) -> dict[str, dict[str, str | None]]:
    if not customer_ids:
        return {}

    rows = (
        await db.execute(
            select(
                Visit.customer_id,
                Visit.visit_date,
                Visit.created_at,
                VisitOrder.jgbm,
                VisitOrder.kutyp_dq,
                VisitOrder.kutyp_dq_txt,
                VisitOrder.khlx,
                VisitOrder.sjrq,
                VisitOrder.crtdt,
                VisitOrder.crttm,
            )
            .join(VisitOrder, Visit.external_visit_order_no == VisitOrder.dzdh)
            .where(
                Visit.customer_id.in_(customer_ids),
                Visit.external_visit_order_no.is_not(None),
                visit_scope_condition(scope),
                visit_order_scope_condition(scope),
            )
        )
    ).all()

    latest: dict[str, tuple[tuple[str, str, str], dict[str, str | None]]] = {}
    for (
        customer_id,
        visit_date,
        visit_created_at,
        institution_code,
        kutyp_dq,
        kutyp_dq_txt,
        khlx,
        order_sjrq,
        order_crtdt,
        order_crttm,
    ) in rows:
        code = _normalize_customer_type_code(kutyp_dq) or _normalize_customer_type_code(khlx)
        label = _normalize_customer_type_label(code, kutyp_dq_txt)
        if not label:
            continue
        payload = {
            "customer_type_code": code,
            "customer_type_label": label,
            "customer_type_institution_code": str(institution_code or "").strip() or None,
        }
        sort_key = _customer_type_sort_key(visit_date, visit_created_at, order_sjrq, order_crtdt, order_crttm)
        existing = latest.get(customer_id)
        if existing is None or sort_key > existing[0]:
            latest[customer_id] = (sort_key, payload)

    return {customer_id: payload for customer_id, (_, payload) in latest.items()}


async def _resolve_customer_type(
    db: AsyncSession,
    customer_id: str,
    scope,
) -> dict[str, str | None] | None:
    return (await _customer_type_map(db, [customer_id], scope)).get(customer_id)


async def _get_scoped_customer(customer_id: str, db: AsyncSession, current_user) -> Customer | None:
    scope = await build_permission_scope(current_user)
    return (
        await db.execute(select(Customer).where(Customer.id == customer_id, customer_scope_condition(scope)))
    ).scalar_one_or_none()


async def _resolve_visible_visit_ids_fast(
    db: AsyncSession,
    scope,
) -> list[str] | None:
    """Materialize visit IDs that satisfy `visit_scope ∩ _visit_has_visible_recordings`.

    For non-global staff roles `_visible_recording_scope_condition` simplifies to
    `Recording.staff_id == scope.staff_id`, and that condition (direct or via link)
    is a strict subset of `visit_scope_condition`. So the combined filter reduces
    to "visits owning a recording (direct or linked) for managed staff".

    For global / hospital-admin roles the recording filter is `true()`, so we
    take the full 5-branch visit_scope union and then intersect (in Python)
    with "visit has any recording" — still much cheaper than the original
    correlated-EXISTS chain.

    Returns None to indicate "no fast path" only when there's no staff_id at all.
    """
    if not scope.staff_id:
        return None
    role = normalize_permission_role(scope.role)
    is_admin = role in {"super_admin", "system_admin"} or scope.role == "hospital_admin"
    managed_ids = await _resolve_managed_staff_ids(db, scope)
    if not managed_ids:
        return []

    if not is_admin:
        # Staff role short-circuit: recordings are a strict subset of visit_scope.
        direct_q = select(Recording.visit_id.label("visit_id")).where(
            Recording.staff_id.in_(managed_ids),
            Recording.visit_id.is_not(None),
        )
        linked_q = (
            select(RecordingVisitLink.visit_id.label("visit_id"))
            .join(Recording, Recording.id == RecordingVisitLink.recording_id)
            .where(Recording.staff_id.in_(managed_ids))
        )
        rows = (await db.execute(union(direct_q, linked_q))).all()
        return [row[0] for row in rows if row[0]]

    # Admin path: full 5-branch visit_scope union ∩ visits-with-any-recording.
    scope_visit_ids = await _resolve_scope_visit_ids_union(db, scope, managed_ids)
    if not scope_visit_ids:
        return []
    rec_rows = (
        await db.execute(
            union(
                select(Recording.visit_id.label("visit_id")).where(Recording.visit_id.is_not(None)),
                select(RecordingVisitLink.visit_id.label("visit_id")),
            )
        )
    ).all()
    visits_with_recordings = {r[0] for r in rec_rows if r[0]}
    return [vid for vid in scope_visit_ids if vid in visits_with_recordings]


async def _resolve_scope_visit_ids_union(db: AsyncSession, scope, managed_ids: list[str]) -> list[str]:
    """Materialize the full `visit_scope_condition` set as a union of indexed lookups."""
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
    from smart_badge_api.db.models import Staff
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


async def _resolve_visible_customer_ids_broad(
    db: AsyncSession,
    scope,
) -> list[str] | None:
    """Materialize the customer set that `customer_scope_condition` would match.

    Replaces the triple-nested correlated EXISTS chain with a single UNION of
    indexed lookups. Returns None only when there's no staff_id at all.
    """
    if not scope.staff_id:
        return None
    # For the staff role, `_staff_id_in_management_scope` reduces to either
    # equality (single_staff) or self ∪ subordinates filtered by role level.
    # We resolve the ID set in Python so the SQL becomes IN-list lookups.
    managed_ids = await _resolve_managed_staff_ids(db, scope)
    if not managed_ids:
        return []
    parts = [
        select(Visit.customer_id.label("customer_id")).where(
            Visit.consultant_id.in_(managed_ids), Visit.customer_id.is_not(None)
        ),
        select(Visit.customer_id.label("customer_id")).where(
            Visit.doctor_id.in_(managed_ids), Visit.customer_id.is_not(None)
        ),
        select(Visit.customer_id.label("customer_id"))
        .join(Recording, Recording.visit_id == Visit.id)
        .where(Recording.staff_id.in_(managed_ids), Visit.customer_id.is_not(None)),
        select(Visit.customer_id.label("customer_id"))
        .join(RecordingVisitLink, RecordingVisitLink.visit_id == Visit.id)
        .join(Recording, Recording.id == RecordingVisitLink.recording_id)
        .where(Recording.staff_id.in_(managed_ids), Visit.customer_id.is_not(None)),
    ]
    # visit_order branch: match by (hospital_code, external_account) pairs.
    from smart_badge_api.db.models import Staff
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
                select(Visit.customer_id.label("customer_id"))
                .join(VisitOrder, VisitOrder.dzdh == Visit.external_visit_order_no)
                .where(
                    Visit.external_visit_order_no.is_not(None),
                    Visit.customer_id.is_not(None),
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
    return [row[0] for row in rows if row[0]]


async def _resolve_managed_staff_ids(db: AsyncSession, scope) -> list[str]:
    """Self + subordinates whose permission_role level <= actor's level."""
    if not scope.staff_id:
        return []
    if scope.role == "single_staff":
        return [scope.staff_id]
    from smart_badge_api.db.models import Staff, StaffManagementRelation
    from smart_badge_api.core.permissions import PERMISSION_ROLE_LEVELS, LEGACY_STAFF_PERMISSION_ROLE_MAP
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


def _scope_visit_filter(scope, visible_visit_ids: list[str] | None):
    """Return the WHERE expression for `visit_scope ∩ _visit_has_visible_recordings`.

    Uses a precomputed IN-list when available for an indexed lookup; otherwise
    falls back to the original correlated-EXISTS form.
    """
    if visible_visit_ids is None:
        return and_only(
            visit_scope_condition(scope),
            _visit_has_visible_recordings_condition(scope),
        )
    if not visible_visit_ids:
        return false()
    return Visit.id.in_(visible_visit_ids)


def and_only(*conds):
    # tiny helper to combine multiple boolean expressions for `.where(*)`
    from sqlalchemy import and_
    return and_(*conds)


def _visit_count_subquery(scope, visible_visit_ids=None):
    return (
        select(Visit.customer_id, func.count(Visit.id).label("visit_count"))
        .where(_scope_visit_filter(scope, visible_visit_ids))
        .group_by(Visit.customer_id)
        .subquery()
    )


def _won_count_subquery(scope, visible_visit_ids=None):
    return (
        select(Visit.customer_id, func.count(Visit.id).label("closed_won_count"))
        .where(Visit.status == "closed_won", _scope_visit_filter(scope, visible_visit_ids))
        .group_by(Visit.customer_id)
        .subquery()
    )


def _last_visit_subquery(scope, visible_visit_ids=None):
    return (
        select(Visit.customer_id, func.max(Visit.visit_date).label("last_visit_at"))
        .where(_scope_visit_filter(scope, visible_visit_ids))
        .group_by(Visit.customer_id)
        .subquery()
    )


def _deposit_principal_subquery(scope, visible_visit_ids=None):
    return (
        select(
            Visit.customer_id,
            func.sum(Visit.deposit_principal).label("total_deposit_principal"),
        )
        .where(_scope_visit_filter(scope, visible_visit_ids))
        .group_by(Visit.customer_id)
        .subquery()
    )


def _visible_recording_scope_condition(scope):
    if normalize_permission_role(scope.role) in {"super_admin", "system_admin"}:
        return true()
    if scope.role == "hospital_admin":
        return true()
    if scope.staff_id:
        return Recording.staff_id == scope.staff_id
    return false()


def _visit_has_visible_recordings_condition(scope, *, visit_model=Visit):
    recording_scope = _visible_recording_scope_condition(scope)
    return or_(
        exists(
            select(Recording.id).where(
                Recording.visit_id == visit_model.id,
                recording_scope,
            )
        ),
        exists(
            select(RecordingVisitLink.id)
            .join(Recording, Recording.id == RecordingVisitLink.recording_id)
            .where(
                RecordingVisitLink.visit_id == visit_model.id,
                recording_scope,
            )
        ),
    )


def _customer_has_scoped_recordings(scope):
    return (
        select(Recording.id)
        .join(Visit, Visit.id == Recording.visit_id)
        .where(
            Visit.customer_id == Customer.id,
            recording_scope_condition(scope),
        )
        .exists()
    )


async def _customer_recording_count_map(
    db: AsyncSession,
    customer_ids: list[str],
    scope,
) -> dict[str, int]:
    if not customer_ids:
        return {}

    direct_recordings = (
        select(
            Visit.customer_id.label("customer_id"),
            Recording.id.label("recording_id"),
        )
        .join(Visit, Visit.id == Recording.visit_id)
        .where(
            Visit.customer_id.in_(customer_ids),
            visit_scope_condition(scope),
            recording_scope_condition(scope),
        )
    )

    linked_recordings = (
        select(
            Visit.customer_id.label("customer_id"),
            RecordingVisitLink.recording_id.label("recording_id"),
        )
        .join(Visit, Visit.id == RecordingVisitLink.visit_id)
        .join(Recording, Recording.id == RecordingVisitLink.recording_id)
        .where(
            Visit.customer_id.in_(customer_ids),
            visit_scope_condition(scope),
            recording_scope_condition(scope),
        )
    )

    recording_rows = union_all(direct_recordings, linked_recordings).subquery()
    rows = (
        await db.execute(
            select(
                recording_rows.c.customer_id,
                func.count(distinct(recording_rows.c.recording_id)).label("recording_count"),
            )
            .group_by(recording_rows.c.customer_id)
        )
    ).all()
    return {customer_id: int(recording_count or 0) for customer_id, recording_count in rows}


def _to_out(
    customer: Customer,
    *,
    visit_count: int,
    recording_count: int = 0,
    closed_won_count: int,
    total_deposit_principal: float | None = None,
    customer_type: dict[str, str | None] | None = None,
    last_visit_at: str | None,
) -> CustomerOut:
    out = CustomerOut.model_validate(customer)
    out.visit_count = visit_count
    out.recording_count = recording_count
    out.closed_won_count = closed_won_count
    out.total_deposit_principal = total_deposit_principal
    if customer_type:
        out.customer_type_code = customer_type.get("customer_type_code")
        out.customer_type_label = customer_type.get("customer_type_label")
        out.customer_type_institution_code = customer_type.get("customer_type_institution_code")
    out.last_visit_at = last_visit_at
    return out


def _build_excerpt(text: str | None, limit: int = 96) -> str | None:
    if not text:
        return None
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3].rstrip()}..."


def _analysis_summary_from_result(result: dict | None) -> str | None:
    payload = normalize_analysis_result(result) or {}
    if not payload:
        return None
    raw_summary = (
        payload.get("consultation_process_evaluation", {}).get("overall_summary")
        or payload.get("consultation_evaluation", {}).get("overall_summary")
    )
    if not isinstance(raw_summary, str):
        return None
    summary = _LEADING_SCORE_SUMMARY_RE.sub("", raw_summary.strip()).strip()
    return summary or None


def _analysis_profile_tags_from_result(result: dict | None) -> list[str]:
    payload = normalize_analysis_result(result) or {}
    if not payload:
        return []

    labels: list[str] = []
    seen: set[str] = set()
    for item in payload.get("customer_profile", {}).get("tags") or []:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip()
        value = str(item.get("value") or "").strip()
        if category in _PROFILE_ANALYSIS_EXCLUDED_CATEGORY_NAMES:
            continue
        if value and _should_ignore_tag_completion_value(category, value):
            continue
        label = f"{category}：{value}" if category and value else value or category
        normalized_label = label.strip()
        if not normalized_label or normalized_label in seen:
            continue
        seen.add(normalized_label)
        labels.append(normalized_label)
    return labels


def _normalize_inline_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _dedupe_analysis_texts(values: list[str], *, limit: int = 4) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _normalize_inline_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
        if len(normalized) >= limit:
            break
    return normalized


def _extract_sap_consultation_texts(payloads: object) -> list[str]:
    if not isinstance(payloads, list):
        return []
    texts: list[str] = []
    seen: set[str] = set()
    for item in payloads:
        if not isinstance(item, dict):
            continue
        text = _normalize_inline_text(item.get("text"))
        if not text or text in seen:
            continue
        seen.add(text)
        texts.append(text)
    return texts


def _fallback_sap_consultation_text(
    *,
    advisor_name: str | None,
    task: AnalysisTask | None,
) -> str | None:
    result_payload = task.result if task and task.status == "done" and isinstance(task.result, dict) else {}
    text = build_consultation_text(advisor_name or "", result_payload)
    normalized = _normalize_inline_text(text)
    return text if normalized else None


def _coerce_analysis_score(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _analysis_primary_demands_from_result(result: dict | None) -> list[str]:
    payload = normalize_analysis_result(result) or {}
    if not payload:
        return []

    primary_demands = payload.get("customer_primary_demands") or {}
    items = primary_demands.get("items") or []
    labels: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        demand = _normalize_inline_text(item.get("demand"))
        if demand:
            labels.append(demand)

    if not labels:
        summary = _normalize_inline_text(primary_demands.get("summary"))
        if summary:
            labels.append(summary)
    return _dedupe_analysis_texts(labels)


def _analysis_concerns_from_result(result: dict | None) -> list[str]:
    payload = normalize_analysis_result(result) or {}
    if not payload:
        return []

    concerns = payload.get("customer_concerns") or {}
    items = concerns.get("items") or []
    labels: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = _normalize_inline_text(item.get("content"))
        if content:
            labels.append(content)

    if not labels:
        summary = _normalize_inline_text(concerns.get("summary"))
        if summary:
            labels.append(summary)
    return _dedupe_analysis_texts(labels)


def _analysis_recommendations_from_result(result: dict | None) -> list[str]:
    payload = normalize_analysis_result(result) or {}
    if not payload:
        return []

    recommendations = payload.get("staff_recommendations") or {}
    items = recommendations.get("items") or []
    labels: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        recommendation = _normalize_inline_text(item.get("recommendation"))
        product_or_solution = _normalize_inline_text(item.get("product_or_solution"))
        if recommendation:
            labels.append(recommendation)
            continue
        if product_or_solution:
            labels.append(product_or_solution)

    if not labels:
        summary = _normalize_inline_text(recommendations.get("summary"))
        if summary:
            labels.append(summary)
    return _dedupe_analysis_texts(labels)


def _analysis_evaluation_dimensions_from_result(
    result: dict | None,
) -> list[CustomerDetailRecordingEvalDimensionOut]:
    payload = normalize_analysis_result(result) or {}
    if not payload:
        return []

    evaluation = payload.get("consultation_evaluation") or {}
    dimensions: list[CustomerDetailRecordingEvalDimensionOut] = []
    for raw in evaluation.get("dimensions") or []:
        if not isinstance(raw, dict):
            continue

        name = _normalize_inline_text(raw.get("name"))
        if not name:
            continue

        point_score = _coerce_analysis_score(raw.get("point_score"))
        if point_score is None:
            legacy_score = _coerce_analysis_score(raw.get("score"))
            if legacy_score is not None:
                point_score = max(0.0, min(legacy_score / 10, 1.0))

        max_score = _coerce_analysis_score(raw.get("max_score"))
        summary = _normalize_inline_text(raw.get("summary")) or None
        if summary is None:
            issue_texts = [
                _normalize_inline_text(issue.get("description"))
                for issue in raw.get("issues") or []
                if isinstance(issue, dict)
            ]
            deduped_issue_texts = _dedupe_analysis_texts(issue_texts, limit=2)
            if deduped_issue_texts:
                summary = "；".join(deduped_issue_texts)

        dimensions.append(
            CustomerDetailRecordingEvalDimensionOut(
                name=name,
                point_score=point_score,
                max_score=max_score if max_score is not None and max_score > 0 else 1.0,
                summary=summary,
            )
        )

    return sorted(dimensions, key=lambda item: (_EVAL_DIMENSION_ORDER.get(item.name, 99), item.name))


def _parse_visit_time(value: str | None) -> time | None:
    if not value:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt).time()
        except ValueError:
            continue
    return None


def _resolve_visit_display_datetime(
    visit_date: date | None,
    visit_time: str | None,
    created_at: datetime | None,
) -> datetime | None:
    if visit_date:
        parsed_time = _parse_visit_time(visit_time)
        if parsed_time is not None:
            return datetime.combine(visit_date, parsed_time, tzinfo=BEIJING_TZ)
    if created_at is not None:
        return created_at
    if visit_date:
        return datetime.combine(visit_date, time.min, tzinfo=BEIJING_TZ)
    return None


def _extract_recording_id(file_name: str) -> str | None:
    if file_name.startswith("recording_") and file_name.endswith(".json"):
        return file_name.removeprefix("recording_").removesuffix(".json")
    return None


def _filter_recordings_for_scope(recordings: list[Recording], scope) -> list[Recording]:
    if normalize_permission_role(scope.role) == "staff" and scope.staff_id:
        return [recording for recording in recordings if recording.staff_id == scope.staff_id]
    return recordings


def _merge_visit_recordings(visit: Visit) -> list[Recording]:
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

    return list(merged.values())


def _task_timestamp(task: AnalysisTask) -> datetime | None:
    return task.completed_at or task.created_at


def _timestamp_key(value: datetime | None) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _task_sort_key(task: AnalysisTask) -> float:
    return _timestamp_key(_task_timestamp(task))


def _quality_label(score: float | None) -> str:
    if score is None:
        return "待分析"
    if score >= 8:
        return "优秀"
    if score >= 6.5:
        return "良好"
    if score >= 5:
        return "待提升"
    return "高风险"


def _coerce_score(task: AnalysisTask) -> float | None:
    if task.overall_score is not None:
        return float(task.overall_score)
    result = task.result or {}
    raw = (
        result.get("consultation_process_evaluation", {}).get("overall_score")
        or result.get("consultation_evaluation", {}).get("overall_score")
    )
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _resolve_timeline_project_name(visit: Visit, visit_orders: list[VisitOrder]) -> str | None:
    if visit.project_needs and visit.project_needs.strip():
        return visit.project_needs.strip()

    project_parts: list[str] = []
    for order in visit_orders:
        project_name = _first_non_empty(order.remark_dz)
        if not project_name:
            continue
        normalized_name = project_name.strip()
        if normalized_name and normalized_name not in project_parts:
            project_parts.append(normalized_name)

    return "、".join(project_parts) if project_parts else None


def _resolve_timeline_deal_amount(visit: Visit, visit_orders: list[VisitOrder]) -> float | None:
    _ = visit_orders
    principal = float(visit.deposit_principal) if visit.deposit_principal is not None else None
    bonus = float(visit.deposit_bonus) if visit.deposit_bonus is not None else None

    if principal is None and bonus is None:
        return None

    return round((principal or 0.0) + (bonus or 0.0), 2)


def _upsert_theme(
    bucket: dict[str, dict[str, str | int | datetime | None]],
    *,
    label: str,
    detail: str | None,
    seen_at: datetime | None,
) -> None:
    normalized_label = label.strip()
    if not normalized_label:
        return

    current = bucket.get(normalized_label)
    if current is None:
        bucket[normalized_label] = {
            "label": normalized_label,
            "detail": detail.strip() if detail else None,
            "count": 1,
            "latest_seen_at": seen_at,
        }
        return

    current["count"] = int(current["count"] or 0) + 1
    if detail and (not current.get("detail") or len(detail) > len(str(current["detail"]))):
        current["detail"] = detail.strip()

    current_seen_at = current.get("latest_seen_at")
    if isinstance(current_seen_at, datetime):
        if seen_at and seen_at > current_seen_at:
            current["latest_seen_at"] = seen_at
    elif seen_at:
        current["latest_seen_at"] = seen_at


def _to_theme_list(
    bucket: dict[str, dict[str, str | int | datetime | None]],
    limit: int = 8,
) -> list[CustomerMergedThemeOut]:
    items = sorted(
        bucket.values(),
        key=lambda item: (
            -int(item.get("count") or 0),
            -_timestamp_key(item.get("latest_seen_at") if isinstance(item.get("latest_seen_at"), datetime) else None),
            str(item.get("label") or ""),
        ),
    )
    return [
        CustomerMergedThemeOut(
            label=str(item.get("label") or ""),
            detail=str(item.get("detail")) if item.get("detail") else None,
            count=int(item.get("count") or 0),
            latest_seen_at=item.get("latest_seen_at").isoformat()
            if isinstance(item.get("latest_seen_at"), datetime)
            else None,
        )
        for item in items[:limit]
    ]


def _build_merged_summary(
    *,
    analyzed_count: int,
    average_score: float | None,
    latest_score: float | None,
    trend: str,
    focus_areas: list[CustomerMergedThemeOut],
    concerns: list[CustomerMergedThemeOut],
    dimensions: list[CustomerMergedDimensionOut],
) -> str:
    if analyzed_count == 0:
        return "该客户还没有可用的分析结果，可先对关联录音发起分析。"

    trend_label = {
        "improving": "回升",
        "declining": "下滑",
    }.get(trend, "平稳")
    parts = [f"已聚合 {analyzed_count} 条已分析录音。"]
    if average_score is not None:
        parts.append(f"平均得分 {average_score:.1f}。")
    if latest_score is not None:
        parts.append(f"最新得分 {latest_score:.1f}，整体趋势{trend_label}。")
    if focus_areas:
        parts.append(f"高频诉求：{'、'.join(item.label for item in focus_areas[:3])}。")
    if concerns:
        parts.append(f"主要顾虑：{'、'.join(item.label for item in concerns[:3])}。")
    weak_dimensions = [item.name for item in dimensions if item.average_score < 6.5][:2]
    if weak_dimensions:
        parts.append(f"需要重点提升：{'、'.join(weak_dimensions)}。")
    return "".join(parts)


def _sort_visits(visits: list[Visit]) -> list[Visit]:
    return sorted(
        visits,
        key=lambda visit: (
            visit.visit_date.isoformat() if visit.visit_date else "",
            visit.visit_time or "",
            visit.created_at.isoformat() if visit.created_at else "",
        ),
        reverse=True,
    )


def _sort_recordings(recordings: list[Recording]) -> list[Recording]:
    return sorted(
        recordings,
        key=lambda recording: recording.created_at.isoformat() if recording.created_at else "",
        reverse=True,
    )


def _date_summary_key(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text[:10] if text else None


def _build_customer_date_summaries(rows: list[tuple[object, int]]) -> list[CustomerDateSummaryOut]:
    summaries = [
        CustomerDateSummaryOut(date=_date_summary_key(value), total=int(total or 0))
        for value, total in rows
    ]
    summaries.sort(key=lambda item: (item.date is not None, item.date or ""), reverse=True)
    return summaries


@router.get("", response_model=CustomerPageOut)
async def list_customers(
    keyword: str = Query("", description="按客户编码或客户姓名搜索"),
    is_active: bool | None = Query(None),
    consultant_id: str | None = Query(None),
    has_visits: bool | None = Query(None),
    has_recordings: bool | None = Query(None),
    has_positive_recharge: bool | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    include_date_summaries: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    scope = await build_permission_scope(current_user)
    # Pre-materialize the (visit_scope ∩ visible_recordings) set so the heavy
    # correlated EXISTS chain collapses to a single indexed IN-list lookup.
    visible_visit_ids = await _resolve_visible_visit_ids_fast(db, scope)
    # Broad customer set (matches `customer_scope_condition` semantics).
    visible_customer_ids = await _resolve_visible_customer_ids_broad(db, scope)
    visit_count_sub = _visit_count_subquery(scope, visible_visit_ids)
    won_count_sub = _won_count_subquery(scope, visible_visit_ids)
    last_visit_sub = _last_visit_subquery(scope, visible_visit_ids)
    deposit_principal_sub = _deposit_principal_subquery(scope, visible_visit_ids)
    scoped_recordings_exists = _customer_has_scoped_recordings(scope)
    customer_activity_date = func.coalesce(last_visit_sub.c.last_visit_at, func.date(Customer.created_at))

    visit_count = func.coalesce(visit_count_sub.c.visit_count, 0)
    closed_won_count = func.coalesce(won_count_sub.c.closed_won_count, 0)
    total_deposit_principal = deposit_principal_sub.c.total_deposit_principal

    stmt = (
        select(
            Customer,
            visit_count.label("visit_count"),
            closed_won_count.label("closed_won_count"),
            total_deposit_principal.label("total_deposit_principal"),
            last_visit_sub.c.last_visit_at.label("last_visit_at"),
        )
        .outerjoin(visit_count_sub, Customer.id == visit_count_sub.c.customer_id)
        .outerjoin(won_count_sub, Customer.id == won_count_sub.c.customer_id)
        .outerjoin(deposit_principal_sub, Customer.id == deposit_principal_sub.c.customer_id)
        .outerjoin(last_visit_sub, Customer.id == last_visit_sub.c.customer_id)
        .where(
            customer_scope_condition(scope)
            if visible_customer_ids is None
            else (Customer.id.in_(visible_customer_ids) if visible_customer_ids else false())
        )
        .order_by(last_visit_sub.c.last_visit_at.desc().nulls_last(), Customer.created_at.desc())
    )

    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                Customer.name.ilike(like),
                Customer.external_customer_code.ilike(like),
            )
        )
    if is_active is not None:
        stmt = stmt.where(Customer.is_active == is_active)
    if consultant_id:
        if visible_visit_ids is None:
            stmt = stmt.where(
                select(Visit.id)
                .where(
                    Visit.customer_id == Customer.id,
                    Visit.consultant_id == consultant_id,
                    visit_scope_condition(scope),
                    _visit_has_visible_recordings_condition(scope),
                )
                .exists()
            )
        elif visible_visit_ids:
            stmt = stmt.where(
                select(Visit.id)
                .where(
                    Visit.customer_id == Customer.id,
                    Visit.consultant_id == consultant_id,
                    Visit.id.in_(visible_visit_ids),
                )
                .exists()
            )
        else:
            stmt = stmt.where(false())
    if has_recordings is True:
        stmt = stmt.where(scoped_recordings_exists)
    if has_recordings is False:
        stmt = stmt.where(~scoped_recordings_exists)
    if has_visits is True:
        stmt = stmt.where(visit_count > 0)
    if has_visits is False:
        stmt = stmt.where(visit_count == 0)
    if has_positive_recharge is True:
        stmt = stmt.where(total_deposit_principal > 0)
    if has_positive_recharge is False:
        stmt = stmt.where(or_(total_deposit_principal <= 0, total_deposit_principal.is_(None)))
    if date_from:
        stmt = stmt.where(customer_activity_date >= date_from)
    if date_to:
        stmt = stmt.where(customer_activity_date <= date_to)

    filtered_subquery = stmt.order_by(None).subquery()
    total = (await db.execute(select(func.count()).select_from(filtered_subquery))).scalar_one()
    if include_date_summaries:
        date_summary_rows = (
            await db.execute(
                select(filtered_subquery.c.last_visit_at, func.count())
                .group_by(filtered_subquery.c.last_visit_at)
            )
        ).all()
        date_summaries = _build_customer_date_summaries(date_summary_rows)
    else:
        date_summaries = []
    rows = (await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))).all()
    customer_ids = [customer.id for customer, *_ in rows]
    latest_visit_map: dict[str, datetime] = {}
    recording_count_map: dict[str, int] = {}

    if customer_ids:
        if visible_visit_ids is None:
            latest_visits_where = and_only(
                Visit.customer_id.in_(customer_ids),
                visit_scope_condition(scope),
                _visit_has_visible_recordings_condition(scope),
            )
        elif visible_visit_ids:
            latest_visits_where = and_only(
                Visit.customer_id.in_(customer_ids),
                Visit.id.in_(visible_visit_ids),
            )
        else:
            latest_visits_where = false()
        latest_visits = (
            await db.execute(
                select(Visit.customer_id, Visit.visit_date, Visit.visit_time, Visit.created_at).where(
                    latest_visits_where
                )
            )
        ).all()
        for customer_id, visit_date, visit_time, created_at in latest_visits:
            resolved_at = _resolve_visit_display_datetime(visit_date, visit_time, created_at)
            if resolved_at is None:
                continue
            current = latest_visit_map.get(customer_id)
            if current is None or _timestamp_key(resolved_at) > _timestamp_key(current):
                latest_visit_map[customer_id] = resolved_at
        recording_count_map = await _customer_recording_count_map(db, customer_ids, scope)
    customer_type_map = await _customer_type_map(db, customer_ids, scope)

    results = [
        _to_out(
            customer,
            visit_count=int(customer_visit_count or 0),
            recording_count=recording_count_map.get(customer.id, 0),
            closed_won_count=int(customer_closed_won_count or 0),
            total_deposit_principal=(
                float(customer_total_deposit_principal)
                if customer_total_deposit_principal is not None
                else None
            ),
            customer_type=customer_type_map.get(customer.id),
            last_visit_at=(
                latest_visit_map.get(customer.id).isoformat()
                if latest_visit_map.get(customer.id)
                else last_visit_at.isoformat() if last_visit_at else None
            ),
        )
        for customer, customer_visit_count, customer_closed_won_count, customer_total_deposit_principal, last_visit_at in rows
    ]
    page_response = make_page_response(results, total, page, page_size)
    return CustomerPageOut(
        items=page_response.items,
        total=page_response.total,
        page=page_response.page,
        page_size=page_response.page_size,
        pages=page_response.pages,
        date_summaries=date_summaries,
    )


@router.get("/{customer_id}", response_model=CustomerOut)
async def get_customer(
    customer_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    scope = await build_permission_scope(current_user)
    customer = await _get_scoped_customer(customer_id, db, current_user)
    if not customer:
        raise HTTPException(404, "客户不存在")

    visit_count = (
        await db.execute(
            select(func.count(Visit.id)).where(
                Visit.customer_id == customer_id,
                visit_scope_condition(scope),
                _visit_has_visible_recordings_condition(scope),
            )
        )
    ).scalar_one()
    closed_won_count = (
        await db.execute(
            select(func.count(Visit.id)).where(
                Visit.customer_id == customer_id,
                Visit.status == "closed_won",
                visit_scope_condition(scope),
                _visit_has_visible_recordings_condition(scope),
            )
        )
    ).scalar_one()
    latest_visit = (
        await db.execute(
            select(Visit.visit_date, Visit.visit_time, Visit.created_at)
            .where(
                Visit.customer_id == customer_id,
                visit_scope_condition(scope),
                _visit_has_visible_recordings_condition(scope),
            )
            .order_by(Visit.visit_date.desc(), Visit.visit_time.desc(), Visit.created_at.desc())
            .limit(1)
        )
    ).first()
    last_visit_at = (
        _resolve_visit_display_datetime(latest_visit[0], latest_visit[1], latest_visit[2])
        if latest_visit
        else None
    )
    total_deposit_principal = (
        await db.execute(
            select(func.sum(Visit.deposit_principal)).where(
                Visit.customer_id == customer_id,
                visit_scope_condition(scope),
                _visit_has_visible_recordings_condition(scope),
            )
        )
    ).scalar_one()

    return _to_out(
        customer,
        visit_count=int(visit_count or 0),
        closed_won_count=int(closed_won_count or 0),
        total_deposit_principal=float(total_deposit_principal) if total_deposit_principal is not None else None,
        customer_type=await _resolve_customer_type(db, customer_id, scope),
        last_visit_at=last_visit_at.isoformat() if last_visit_at else None,
    )


@router.get("/{customer_id}/detail", response_model=CustomerDetailOut)
async def get_customer_detail(
    customer_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    scope = await build_permission_scope(current_user)
    customer = (
        await db.execute(
            select(Customer)
            .where(Customer.id == customer_id, customer_scope_condition(scope))
        )
    ).scalar_one_or_none()
    if not customer:
        raise HTTPException(404, "客户不存在")

    visits = _sort_visits(
        (
            await db.execute(
                select(Visit)
                .where(Visit.customer_id == customer_id, visit_scope_condition(scope))
                .options(
                    selectinload(Visit.consultant),
                    selectinload(Visit.doctor),
                    selectinload(Visit.recordings).selectinload(Recording.transcript),
                    selectinload(Visit.recordings).selectinload(Recording.staff),
                    selectinload(Visit.recording_links).selectinload(RecordingVisitLink.recording).selectinload(Recording.transcript),
                    selectinload(Visit.recording_links).selectinload(RecordingVisitLink.recording).selectinload(Recording.staff),
                )
            )
        ).scalars().all()
    )
    visible_recordings_by_visit_id_all = {
        visit.id: _sort_recordings(_filter_recordings_for_scope(_merge_visit_recordings(visit), scope))
        for visit in visits
    }
    visits = [visit for visit in visits if visible_recordings_by_visit_id_all.get(visit.id)]
    visible_recordings_by_visit_id = {
        visit.id: visible_recordings_by_visit_id_all[visit.id]
        for visit in visits
    }
    pending_archive_recordings_by_visit_id = await build_pending_archive_recordings_by_visit_id(db, visits, scope)
    recordings = list({
        recording.id: recording
        for visible_recordings in visible_recordings_by_visit_id.values()
        for recording in visible_recordings
    }.values())
    visit_order_map: dict[str, list[VisitOrder]] = {}
    visit_order_nos = list({visit.external_visit_order_no for visit in visits if visit.external_visit_order_no})
    if visit_order_nos:
        visit_orders = (
            await db.execute(
                select(VisitOrder)
                .where(
                    VisitOrder.dzdh.in_(visit_order_nos),
                    visit_order_scope_condition(scope),
                )
                .order_by(VisitOrder.dzdh.desc(), VisitOrder.fzsj.desc(), VisitOrder.dzseg.asc())
            )
        ).scalars().all()
        for order in visit_orders:
            if not order.dzdh:
                continue
            visit_order_map.setdefault(order.dzdh, []).append(order)
    file_name_map = {f"recording_{recording.id}.json": recording.id for recording in recordings}
    task_by_recording_id: dict[str, AnalysisTask] = {}
    if file_name_map:
        tasks = (
            await db.execute(
                select(AnalysisTask)
                .where(AnalysisTask.file_name.in_(list(file_name_map.keys())))
                .order_by(AnalysisTask.completed_at.desc(), AnalysisTask.created_at.desc())
            )
        ).scalars().all()
        for task in tasks:
            recording_id = file_name_map.get(task.file_name)
            if recording_id and recording_id not in task_by_recording_id:
                task_by_recording_id[recording_id] = task

    sap_texts_by_recording_id: dict[str, list[str]] = {}
    if recordings:
        sap_logs = (
            await db.execute(
                select(SapPushLog)
                .where(SapPushLog.recording_id.in_([recording.id for recording in recordings]))
                .order_by(SapPushLog.created_at.desc())
            )
        ).scalars().all()
        for log in sap_logs:
            if not log.recording_id:
                continue
            bucket = sap_texts_by_recording_id.setdefault(log.recording_id, [])
            existing = set(bucket)
            for text in _extract_sap_consultation_texts(log.request_payloads):
                if text in existing:
                    continue
                existing.add(text)
                bucket.append(text)

    visit_items: list[CustomerDetailVisitOut] = []
    transcript_count = 0
    analyzed_recording_count = 0

    for visit in visits:
        recording_items: list[CustomerDetailRecordingOut] = []
        visit_recordings = visible_recordings_by_visit_id.get(visit.id, [])
        visit_orders_for_summary = visit_order_map.get(visit.external_visit_order_no or "", [])
        advisor_name_for_visit = _first_non_empty(
            visit_orders_for_summary[0].advxc_long if visit_orders_for_summary else None,
            visit_orders_for_summary[0].fzuer_long if visit_orders_for_summary else None,
            visit.consultant.name if visit.consultant else None,
        )
        for recording in visible_recordings_by_visit_id.get(visit.id, []):
            transcript = recording.transcript
            task = task_by_recording_id.get(recording.id)
            analysis_summary = _analysis_summary_from_result(task.result) if task and task.status == "done" else None
            analysis_profile_tags = _analysis_profile_tags_from_result(task.result) if task and task.status == "done" else []
            analysis_primary_demands = _analysis_primary_demands_from_result(task.result) if task and task.status == "done" else []
            analysis_concerns = _analysis_concerns_from_result(task.result) if task and task.status == "done" else []
            analysis_recommendations = _analysis_recommendations_from_result(task.result) if task and task.status == "done" else []
            analysis_evaluation_dimensions = (
                _analysis_evaluation_dimensions_from_result(task.result) if task and task.status == "done" else []
            )
            if transcript and transcript.status == "completed":
                transcript_count += 1
            if task and task.status == "done":
                analyzed_recording_count += 1

            recording_items.append(
                CustomerDetailRecordingOut(
                    id=recording.id,
                    visit_id=recording.visit_id,
                    file_name=recording.file_name,
                    device_id=recording.device_id,
                    staff_name=recording.staff.name if recording.staff else None,
                    status=recording.status,
                    duration_seconds=recording.duration_seconds,
                    created_at=recording.created_at,
                    transcript_id=transcript.id if transcript else None,
                    transcript_status=transcript.status if transcript else None,
                    transcript_provider=transcript.asr_provider if transcript else None,
                    transcript_excerpt=_build_excerpt(transcript.full_text if transcript else None),
                    analysis_task_id=task.id if task else None,
                    analysis_status=task.status if task else None,
                    analysis_overall_score=float(task.overall_score)
                    if task and task.overall_score is not None
                    else None,
                    analysis_completed_at=task.completed_at.isoformat() if task and task.completed_at else None,
                    analysis_summary=analysis_summary,
                    analysis_profile_tags=analysis_profile_tags,
                    analysis_primary_demands=analysis_primary_demands,
                    analysis_concerns=analysis_concerns,
                    analysis_recommendations=analysis_recommendations,
                    analysis_evaluation_dimensions=analysis_evaluation_dimensions,
                )
            )

        visit_sap_texts: list[str] = []
        sorted_visit_recordings = sorted(
            visit_recordings,
            key=lambda item: item.created_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        for recording in sorted_visit_recordings:
            existing_texts = list(sap_texts_by_recording_id.get(recording.id, []))
            for text in existing_texts:
                normalized = _normalize_inline_text(text)
                if not normalized:
                    continue
                visit_sap_texts = [text]
                break
            if visit_sap_texts:
                break

        if not visit_sap_texts:
            fallback_text = _fallback_sap_consultation_text(
                advisor_name=advisor_name_for_visit,
                task=next(
                    (
                        task_by_recording_id.get(recording.id)
                        for recording in sorted_visit_recordings
                        if task_by_recording_id.get(recording.id)
                    ),
                    None,
                ),
            )
            if fallback_text:
                visit_sap_texts.append(fallback_text)

        visit_items.append(
            CustomerDetailVisitOut(
                id=visit.id,
                status=visit.status,
                external_visit_order_no=visit.external_visit_order_no,
                visit_date=visit.visit_date,
                visit_time=visit.visit_time,
                consultant_name=visit.consultant.name if visit.consultant else None,
                doctor_name=visit.doctor.name if visit.doctor else None,
                deal_status=visit.deal_status,
                deposit_principal=float(visit.deposit_principal) if visit.deposit_principal is not None else None,
                deposit_bonus=float(visit.deposit_bonus) if visit.deposit_bonus is not None else None,
                arrival_purpose=visit.arrival_purpose,
                project_needs=visit.project_needs,
                notes=visit.notes,
                created_at=visit.created_at,
                recordings=recording_items,
                pending_archive_recordings=pending_archive_recordings_by_visit_id.get(visit.id, []),
                sap_consultation_texts=visit_sap_texts,
                visit_order_summary=_build_customer_detail_visit_order_summary(
                    visit_orders_for_summary
                ),
            )
        )

    visit_count = len(visits)
    closed_won_count = sum(1 for visit in visits if visit.status == "closed_won")
    last_visit_at = max(
        (
            resolved
            for resolved in (
                _resolve_visit_display_datetime(visit.visit_date, visit.visit_time, visit.created_at)
                for visit in visits
            )
            if resolved is not None
        ),
        default=None,
        key=_timestamp_key,
    )

    base = _to_out(
        customer,
        visit_count=visit_count,
        closed_won_count=closed_won_count,
        total_deposit_principal=(
            round(sum(float(visit.deposit_principal) for visit in visits if visit.deposit_principal is not None), 2)
            if any(visit.deposit_principal is not None for visit in visits)
            else None
        ),
        customer_type=await _resolve_customer_type(db, customer_id, scope),
        last_visit_at=last_visit_at.isoformat() if last_visit_at else None,
    )
    return CustomerDetailOut(
        **base.model_dump(exclude={"recording_count"}),
        recording_count=len(recordings),
        transcript_count=transcript_count,
        analyzed_recording_count=analyzed_recording_count,
        visits=visit_items,
    )


@router.get("/{customer_id}/merged-analysis", response_model=CustomerMergedAnalysisOut)
async def get_customer_merged_analysis(
    customer_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    scope = await build_permission_scope(current_user)
    customer = (
        await db.execute(
            select(Customer)
            .where(Customer.id == customer_id, customer_scope_condition(scope))
        )
    ).scalar_one_or_none()
    if not customer:
        raise HTTPException(404, "客户不存在")

    visits = (
        await db.execute(
            select(Visit)
            .where(Visit.customer_id == customer_id, visit_scope_condition(scope))
            .options(selectinload(Visit.recordings))
        )
    ).scalars().all()
    visit_map = {visit.id: visit for visit in visits}
    recordings = [
        recording
        for visit in visits
        for recording in _filter_recordings_for_scope(list(visit.recordings), scope)
    ]
    recording_map = {recording.id: recording for recording in recordings}
    analysis_file_names = [f"recording_{recording.id}.json" for recording in recordings]
    visit_order_map: dict[str, list[VisitOrder]] = {}

    visit_order_nos = list({visit.external_visit_order_no for visit in visits if visit.external_visit_order_no})
    if visit_order_nos:
        visit_orders = (
            await db.execute(
                select(VisitOrder).where(
                    VisitOrder.dzdh.in_(visit_order_nos),
                    visit_order_scope_condition(scope),
                )
            )
        ).scalars().all()
        for order in visit_orders:
            if order.dzdh:
                visit_order_map.setdefault(order.dzdh, []).append(order)

    latest_task_by_recording: dict[str, AnalysisTask] = {}
    if analysis_file_names:
        tasks = (
            await db.execute(
                select(AnalysisTask)
                .where(AnalysisTask.status == "done", AnalysisTask.file_name.in_(analysis_file_names))
                .order_by(AnalysisTask.completed_at.desc(), AnalysisTask.created_at.desc())
            )
        ).scalars().all()
        for task in tasks:
            recording_id = _extract_recording_id(task.file_name)
            if recording_id and recording_id in recording_map and recording_id not in latest_task_by_recording:
                latest_task_by_recording[recording_id] = task

    merged_tasks = sorted(latest_task_by_recording.values(), key=_task_sort_key, reverse=True)
    if not merged_tasks:
        return CustomerMergedAnalysisOut(
            customer_id=customer.id,
            customer_name=customer.name,
            total_visits=len(visits),
            total_recordings=len(recordings),
            analyzed_recordings=0,
            merged_summary="该客户还没有可用的分析结果，可先对关联录音发起分析。",
        )

    focus_bucket: dict[str, dict[str, str | int | datetime | None]] = {}
    concern_bucket: dict[str, dict[str, str | int | datetime | None]] = {}
    tag_bucket: dict[str, dict[str, str | int | datetime | None]] = {}
    dimension_bucket: dict[str, dict[str, str | int | float | datetime | None]] = {}
    scores: list[float] = []
    timeline: list[CustomerMergedTimelineOut] = []

    for task in merged_tasks:
        recording_id = _extract_recording_id(task.file_name)
        recording = recording_map.get(recording_id or "")
        visit = visit_map.get(recording.visit_id or "") if recording else None
        visit_orders = visit_order_map.get(visit.external_visit_order_no or "", []) if visit else []
        result = normalize_analysis_result(task.result) or {}
        seen_at = _task_timestamp(task)
        score = _coerce_score(task)
        if score is not None:
            scores.append(score)

        timeline.append(
            CustomerMergedTimelineOut(
                task_id=task.id,
                recording_id=recording.id if recording else None,
                recording_name=recording.file_name if recording else None,
                visit_id=recording.visit_id if recording else None,
                visit_status=visit.status if visit else None,
                project_name=_resolve_timeline_project_name(visit, visit_orders) if visit else None,
                deal_amount=_resolve_timeline_deal_amount(visit, visit_orders) if visit else None,
                overall_score=score,
                quality_label=_quality_label(score),
                completed_at=seen_at.isoformat() if seen_at else None,
            )
        )

        demands = result.get("customer_demands") or {}
        for item in demands.get("focus_areas") or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("area") or "").strip()
            detail = str(
                item.get("surface_need") or item.get("deep_need") or item.get("discovery_process") or ""
            ).strip()
            _upsert_theme(focus_bucket, label=label, detail=detail or None, seen_at=seen_at)

        concerns = result.get("customer_concerns") or {}
        for item in concerns.get("items") or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("type") or item.get("content") or "").strip()
            detail = str(item.get("content") or item.get("evidence") or "").strip()
            _upsert_theme(concern_bucket, label=label, detail=detail or None, seen_at=seen_at)

        profile = result.get("customer_profile") or {}
        for item in profile.get("tags") or []:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "").strip()
            value = str(item.get("value") or "").strip()
            label = f"{category}：{value}" if category and value else value or category
            _upsert_theme(tag_bucket, label=label, detail=value or None, seen_at=seen_at)

        evaluation = result.get("consultation_evaluation") or {}
        for item in evaluation.get("dimensions") or []:
            if not isinstance(item, dict):
                continue
            name = normalize_consultation_dimension_name(item.get("name")).strip()
            if not name:
                continue
            try:
                dimension_score = float(item.get("score") or 0)
            except (TypeError, ValueError):
                dimension_score = 0.0
            comment = str(item.get("comment") or "").strip()

            current = dimension_bucket.get(name)
            if current is None:
                dimension_bucket[name] = {
                    "name": name,
                    "total_score": dimension_score,
                    "mention_count": 1,
                    "latest_score": dimension_score,
                    "latest_comment": comment or None,
                    "latest_seen_at": seen_at,
                }
                continue

            current["total_score"] = float(current.get("total_score") or 0) + dimension_score
            current["mention_count"] = int(current.get("mention_count") or 0) + 1
            current_seen_at = current.get("latest_seen_at")
            if not isinstance(current_seen_at, datetime) or (seen_at and seen_at >= current_seen_at):
                current["latest_seen_at"] = seen_at
                current["latest_score"] = dimension_score
                current["latest_comment"] = comment or None

    average_score = round(sum(scores) / len(scores), 2) if scores else None
    latest_score = scores[0] if scores else None
    oldest_score = scores[-1] if len(scores) > 1 else None
    score_delta = (
        round(latest_score - oldest_score, 2)
        if latest_score is not None and oldest_score is not None
        else None
    )
    score_trend = "stable"
    if score_delta is not None:
        if score_delta >= 0.5:
            score_trend = "improving"
        elif score_delta <= -0.5:
            score_trend = "declining"

    recurring_focus_areas = _to_theme_list(focus_bucket)
    recurring_concerns = _to_theme_list(concern_bucket)
    profile_tags = normalize_profile_themes(_to_theme_list(tag_bucket))
    dimension_averages = sorted(
        [
            CustomerMergedDimensionOut(
                name=str(item.get("name") or ""),
                average_score=round(
                    float(item.get("total_score") or 0) / max(int(item.get("mention_count") or 1), 1),
                    2,
                ),
                latest_score=float(item.get("latest_score")) if item.get("latest_score") is not None else None,
                mention_count=int(item.get("mention_count") or 0),
                latest_comment=str(item.get("latest_comment")) if item.get("latest_comment") else None,
            )
            for item in dimension_bucket.values()
        ],
        key=lambda item: (item.average_score, -item.mention_count, item.name),
    )

    latest_task = merged_tasks[0]
    latest_recording_id = _extract_recording_id(latest_task.file_name)
    latest_analyzed_at = _task_timestamp(latest_task)

    return CustomerMergedAnalysisOut(
        customer_id=customer.id,
        customer_name=customer.name,
        total_visits=len(visits),
        total_recordings=len(recordings),
        analyzed_recordings=len(merged_tasks),
        average_score=average_score,
        latest_score=latest_score,
        score_delta=score_delta,
        score_trend=score_trend,
        merged_summary=_build_merged_summary(
            analyzed_count=len(merged_tasks),
            average_score=average_score,
            latest_score=latest_score,
            trend=score_trend,
            focus_areas=recurring_focus_areas,
            concerns=recurring_concerns,
            dimensions=dimension_averages,
        ),
        latest_task_id=latest_task.id,
        latest_recording_id=latest_recording_id,
        last_analyzed_at=latest_analyzed_at.isoformat() if latest_analyzed_at else None,
        recurring_focus_areas=recurring_focus_areas,
        recurring_concerns=recurring_concerns,
        profile_tags=profile_tags,
        dimension_averages=dimension_averages,
        timeline=timeline[:6],
    )


@router.get("/{customer_id}/tag-completion", response_model=TagCompletionOut)
async def get_customer_tag_completion(
    customer_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """获取客户标签提取完成度 — 对照完整标签目录，显示哪些标签已从录音分析中提取到。"""
    scope = await build_permission_scope(current_user)
    customer = (
        await db.execute(
            select(Customer)
            .where(Customer.id == customer_id, customer_scope_condition(scope))
        )
    ).scalar_one_or_none()
    if not customer:
        raise HTTPException(404, "客户不存在")

    # 1. 加载完整标签目录
    await ensure_tag_categories(db)
    categories = (
        await db.execute(
            select(TagCategory)
            .where(TagCategory.is_active.is_(True))
            .options(selectinload(TagCategory.tags))
            .order_by(TagCategory.sort_order)
        )
    ).scalars().all()
    categories = [cat for cat in categories if cat.name not in _PROFILE_ANALYSIS_EXCLUDED_CATEGORY_NAMES]

    # 2. 收集该客户所有录音的分析结果
    visits = (
        await db.execute(
            select(Visit)
            .where(Visit.customer_id == customer_id, visit_scope_condition(scope))
            .options(
                selectinload(Visit.recordings),
                selectinload(Visit.recording_links).selectinload(RecordingVisitLink.recording),
            )
        )
    ).scalars().all()
    recordings = list(
        {
            recording.id: recording
            for visit in visits
            for recording in _filter_recordings_for_scope(_merge_visit_recordings(visit), scope)
        }.values()
    )
    file_names = [f"recording_{r.id}.json" for r in recordings]
    extracted_map: dict[str, dict] = {}  # category_name -> {values, evidence, seen_at}

    if file_names:
        tasks = (
            await db.execute(
                select(AnalysisTask)
                .where(AnalysisTask.status == "done", AnalysisTask.file_name.in_(file_names))
            )
        ).scalars().all()

        for task in tasks:
            result = normalize_analysis_result(task.result) or {}
            profile = result.get("customer_profile") or {}
            seen_at = _task_timestamp(task)
            for item in profile.get("tags") or []:
                if not isinstance(item, dict):
                    continue
                category = str(item.get("category") or "").strip()
                value = str(item.get("value") or "").strip()
                evidence = str(item.get("evidence") or "").strip()
                if not category or not value:
                    continue
                if _should_ignore_tag_completion_value(category, value):
                    continue
                existing = extracted_map.get(category)
                if category == NEGATIVE_PROJECT_TAG_CATEGORY and existing is not None:
                    existing_values = existing["values"]
                    has_concrete_value = any(v != NEGATIVE_PROJECT_EMPTY_VALUE for v in existing_values)
                    if value == NEGATIVE_PROJECT_EMPTY_VALUE and has_concrete_value:
                        continue
                    if value != NEGATIVE_PROJECT_EMPTY_VALUE and NEGATIVE_PROJECT_EMPTY_VALUE in existing_values:
                        existing_values.discard(NEGATIVE_PROJECT_EMPTY_VALUE)
                if existing is None:
                    extracted_map[category] = {
                        "values": {value},
                        "evidence": evidence or None,
                        "seen_at": seen_at,
                    }
                else:
                    existing["values"].add(value)
                    if evidence and (not existing["evidence"] or len(evidence) > len(existing["evidence"])):
                        existing["evidence"] = evidence
                    if seen_at and (not existing["seen_at"] or seen_at > existing["seen_at"]):
                        existing["seen_at"] = seen_at

    archive_birthdate = await _resolve_customer_archive_birthdate(db, customer, scope)
    if archive_birthdate:
        extracted_map[BIRTHDATE_TAG_CATEGORY] = {
            "values": {archive_birthdate},
            "evidence": _CUSTOMER_ARCHIVE_TAG_EVIDENCE,
            "seen_at": customer.updated_at or customer.created_at,
        }

    # 3. 构建结果
    items: list[TagExtractionItem] = []
    extracted_count = 0

    for cat in categories:
        active_tags = [t.name for t in cat.tags if t.is_active]
        # 尝试匹配: 完全匹配或包含匹配
        match = extracted_map.get(cat.name)
        if not match:
            # 尝试部分匹配 (分析结果中 category 可能带前缀如 "客户求美需求_眼部需求")
            for key, val in extracted_map.items():
                if cat.name in key or key in cat.name:
                    match = val
                    break

        if match:
            extracted_count += 1
            items.append(TagExtractionItem(
                category_id=cat.id,
                category_name=cat.name,
                weight_level=cat.weight_level,
                available_tags=active_tags,
                extracted_values=sorted(match["values"]),
                evidence=match.get("evidence"),
                status="extracted",
                last_seen_at=match["seen_at"].isoformat() if match.get("seen_at") else None,
            ))
        else:
            items.append(TagExtractionItem(
                category_id=cat.id,
                category_name=cat.name,
                weight_level=cat.weight_level,
                available_tags=active_tags,
                status="not_extracted",
            ))

    total = len(categories)
    return TagCompletionOut(
        customer_id=customer.id,
        total_categories=total,
        extracted_categories=extracted_count,
        completion_rate=round(extracted_count / total, 3) if total else 0.0,
        categories=items,
    )


def _first_non_empty(*values: str | None) -> str | None:
    for v in values:
        if v and v.strip():
            return v.strip()
    return None


def _build_customer_detail_visit_order_summary(items: list[VisitOrder]) -> CustomerDetailVisitOrderSummaryOut | None:
    if not items:
        return None

    primary = items[0]
    line_items: list[CustomerDetailVisitOrderLineItemOut] = []
    seen_fzdh: set[str] = set()
    for item in items:
        dedupe_key = item.fzdh or f"{item.dzdh}:{item.dzseg or len(line_items)}"
        if dedupe_key in seen_fzdh:
            continue
        seen_fzdh.add(dedupe_key)
        line_items.append(
            CustomerDetailVisitOrderLineItemOut(
                fzdh=item.fzdh,
                advxc_long=item.advxc_long,
                assxc=item.assxc,
                fzsj=item.fzsj,
                fzsta_txt=item.fzsta_txt,
                jcsta_txt=item.jcsta_txt,
            )
        )

    return CustomerDetailVisitOrderSummaryOut(
        dzdh=primary.dzdh,
        jgbm=primary.jgbm,
        crtdt=primary.crtdt,
        crttm=primary.crttm,
        dzsta_txt=primary.dzsta_txt,
        dzly_txt=primary.dzly_txt,
        dymd_txt=primary.dymd_txt,
        dztyp_txt=primary.dztyp_txt,
        jgks_txt=_first_non_empty(primary.jgks_txt, primary.jgks),
        fzuer_long=primary.fzuer_long,
        vipkf=primary.vipkf,
        kulvl_dq=primary.kulvl_dq,
        kutyp_dq_txt=primary.kutyp_dq_txt,
        kut30_dq_txt=primary.kut30_dq_txt,
        kusta_dq_txt=primary.kusta_dq_txt,
        remark_dz=primary.remark_dz,
        line_items=line_items,
    )


@router.get("/{customer_id}/visit-orders", response_model=CustomerVisitOrdersOut)
async def get_customer_visit_orders(
    customer_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """获取客户的到诊单历史，按 DZDH 分组（合并相同到诊单号的不同行项目）。"""
    customer = await _get_scoped_customer(customer_id, db, current_user)
    if not customer:
        raise HTTPException(404, "客户不存在")

    customer_code = customer.external_customer_code
    if not customer_code:
        return CustomerVisitOrdersOut(customer_id=customer.id)

    orders = (
        await db.execute(
            select(VisitOrder)
            .where(VisitOrder.kunr == customer_code, visit_order_scope_condition(await build_permission_scope(current_user)))
            .order_by(VisitOrder.sjrq.desc(), VisitOrder.fzsj.desc(), VisitOrder.dzseg)
        )
    ).scalars().all()

    if not orders:
        return CustomerVisitOrdersOut(customer_id=customer.id, customer_code=customer_code)

    # 按 DZDH 分组
    groups: dict[str, list[VisitOrder]] = {}
    for order in orders:
        key = order.dzdh or order.id
        groups.setdefault(key, []).append(order)

    visit_groups: list[VisitOrderGroupOut] = []
    for dzdh, items in groups.items():
        primary = items[0]  # 取第一条作为主记录
        sub_items = [
            VisitOrderItemOut(
                dzseg=o.dzseg,
                jcsta_txt=o.jcsta_txt,
                remark_dz=o.remark_dz,
            )
            for o in items
        ]

        visit_groups.append(VisitOrderGroupOut(
            dzdh=dzdh,
            visit_date=primary.sjrq or primary.jzrq,
            consultant_name=_first_non_empty(primary.advxc_long, primary.advyq_name),
            status_text=_first_non_empty(primary.jcsta_txt, primary.dzsta_txt),
            customer_type=primary.khlx,
            customer_type_t30=primary.khlx_t30,
            member_level=primary.kulvl_dq,
            remark=_first_non_empty(primary.remark_dz),
            items=sub_items if len(sub_items) > 1 else [],
        ))

    return CustomerVisitOrdersOut(
        customer_id=customer.id,
        customer_code=customer_code,
        total_visits=len(visit_groups),
        visit_groups=visit_groups,
    )


@router.post("", response_model=CustomerOut, status_code=201)
async def create_customer(
    body: CustomerCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    customer = Customer(**body.model_dump())
    db.add(customer)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "企微外部联系人ID已存在") from None

    await db.refresh(customer)
    return _to_out(customer, visit_count=0, closed_won_count=0, last_visit_at=None)


@router.put("/{customer_id}", response_model=CustomerOut)
async def update_customer(
    customer_id: str,
    body: CustomerUpdate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    customer = await _get_scoped_customer(customer_id, db, current_user)
    if not customer:
        raise HTTPException(404, "客户不存在")
    scope = await build_permission_scope(current_user)

    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(customer, key, value)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "企微外部联系人ID已存在") from None

    await db.refresh(customer)
    visit_count = (
        await db.execute(
            select(func.count(Visit.id)).where(
                Visit.customer_id == customer_id,
                visit_scope_condition(scope),
                _visit_has_visible_recordings_condition(scope),
            )
        )
    ).scalar_one()
    closed_won_count = (
        await db.execute(
            select(func.count(Visit.id)).where(
                Visit.customer_id == customer_id,
                Visit.status == "closed_won",
                visit_scope_condition(scope),
                _visit_has_visible_recordings_condition(scope),
            )
        )
    ).scalar_one()
    latest_visit = (
        await db.execute(
            select(Visit.visit_date, Visit.visit_time, Visit.created_at)
            .where(
                Visit.customer_id == customer_id,
                visit_scope_condition(scope),
                _visit_has_visible_recordings_condition(scope),
            )
            .order_by(Visit.visit_date.desc(), Visit.visit_time.desc(), Visit.created_at.desc())
            .limit(1)
        )
    ).first()
    last_visit_at = (
        _resolve_visit_display_datetime(latest_visit[0], latest_visit[1], latest_visit[2])
        if latest_visit
        else None
    )
    return _to_out(
        customer,
        visit_count=int(visit_count or 0),
        closed_won_count=int(closed_won_count or 0),
        last_visit_at=last_visit_at.isoformat() if last_visit_at else None,
    )


@router.delete("/{customer_id}", status_code=204)
async def delete_customer(
    customer_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    customer = await _get_scoped_customer(customer_id, db, current_user)
    if not customer:
        raise HTTPException(404, "客户不存在")
    await db.delete(customer)
    await db.commit()
