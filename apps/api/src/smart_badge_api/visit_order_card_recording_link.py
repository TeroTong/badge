from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.db.models import (
    Customer,
    Recording,
    Staff,
    Visit,
    VisitOrder,
    VisitOrderAdvisorNotification,
)
from smart_badge_api.visit_linking import ordered_recording_visit_links, sync_recording_visit_links
from smart_badge_api.visit_order_notifications import _resolve_tenant_for_hospital
from smart_badge_api.visit_order_sync import (
    _build_visit_notes,
    _compute_customer_current_age,
    _first_non_empty,
    _format_time,
    _parse_jdrq,
    _visit_created_at_from_order,
    _visit_status_from_order,
)
from smart_badge_api.wecom import send_wecom_text_message


logger = logging.getLogger("smart_badge.visit_order_card_recording_link")

_AUTO_LINK_BEFORE_TOLERANCE = timedelta(minutes=10)
_AUTO_LINK_MAX_WINDOW = timedelta(hours=8)
_RECORDING_DISPLAY_PATTERN = re.compile(r"(?P<name>\d{4}_\d{6}(?:_\d{6})?\.mp3)", re.IGNORECASE)
_TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _clean(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_visit_order_date(value: str | None) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def display_recording_name(recording: Recording) -> str:
    fallback_leaf: str | None = None
    for raw in (recording.file_name, recording.file_path):
        text = str(raw or "").strip()
        if not text:
            continue
        leaf = Path(text).name
        match = _RECORDING_DISPLAY_PATTERN.search(leaf)
        if match:
            return match.group("name")
        if leaf:
            fallback_leaf = fallback_leaf or leaf
    created_at = _aware(recording.created_at)
    if created_at is not None:
        suffix = Path(fallback_leaf or "").suffix.lower() or ".mp3"
        return f"{created_at.astimezone(_TZ_SHANGHAI):%m%d_%H%M%S}{suffix}"
    if fallback_leaf:
        return fallback_leaf
    return recording.id


async def mark_visit_order_card_recording_started(
    db: AsyncSession,
    *,
    log_id: str | None,
    staff_id: str | None,
    device_id: str | None,
    device_code: str | None,
) -> None:
    normalized_log_id = _clean(log_id)
    if not normalized_log_id or normalized_log_id == "sample":
        return

    log = await db.get(VisitOrderAdvisorNotification, normalized_log_id)
    if log is None:
        logger.warning("visit-order card recording start log not found log_id=%s", normalized_log_id)
        return

    log.recording_start_requested_at = _utcnow()
    log.recording_start_staff_id = _clean(staff_id)
    log.recording_start_device_id = _clean(device_id)
    log.recording_start_device_code = _clean(device_code)
    log.auto_link_status = "pending"
    log.auto_link_recording_id = None
    log.auto_linked_at = None
    log.auto_link_message_sent_at = None
    log.auto_link_error_message = None
    await db.commit()


async def _load_visit_order_for_notification(
    db: AsyncSession,
    notification: VisitOrderAdvisorNotification,
) -> VisitOrder | None:
    dzdh = _clean(notification.visit_order_no)
    if not dzdh:
        return None

    stmt = select(VisitOrder).where(VisitOrder.dzdh == dzdh)
    hospital_code = _clean(notification.hospital_code)
    if hospital_code:
        stmt = stmt.where(VisitOrder.jgbm == hospital_code)

    dzseg = _clean(notification.visit_order_seg)
    if dzseg:
        exact = (await db.execute(stmt.where(VisitOrder.dzseg == dzseg).limit(1))).scalar_one_or_none()
        if exact is not None:
            return exact

    return (
        await db.execute(stmt.order_by(VisitOrder.dzseg.asc(), VisitOrder.id.asc()).limit(1))
    ).scalar_one_or_none()


async def _ensure_local_visit_for_visit_order(db: AsyncSession, visit_order: VisitOrder) -> Visit:
    dzdh = _clean(visit_order.dzdh)
    if not dzdh:
        raise ValueError("到诊单缺少单号，无法创建本地接诊")

    group_orders = (
        await db.execute(
            select(VisitOrder)
            .where(VisitOrder.dzdh == dzdh, VisitOrder.jgbm == visit_order.jgbm)
            .order_by(VisitOrder.dzseg.asc(), VisitOrder.fzdh.asc(), VisitOrder.id.asc())
        )
    ).scalars().all()
    if not group_orders:
        group_orders = [visit_order]
    primary_order = group_orders[0]

    visit = (
        await db.execute(
            select(Visit)
            .where(
                Visit.external_visit_order_no == dzdh,
                Visit.external_visit_order_seg == primary_order.dzseg,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if visit is None:
        visit = (
            await db.execute(
                select(Visit)
                .where(Visit.external_visit_order_no == dzdh)
                .order_by(Visit.created_at.asc(), Visit.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

    customer_code = _clean(primary_order.kunr)
    customer_name = _clean(primary_order.ninam) or f"客户 {dzdh}"
    customer = await db.get(Customer, visit.customer_id) if visit is not None else None
    if customer is None and customer_code:
        customer = (
            await db.execute(
                select(Customer).where(Customer.external_customer_code == customer_code).limit(1)
            )
        ).scalar_one_or_none()
    if customer is None:
        customer = Customer(
            name=customer_name,
            external_customer_code=customer_code,
            gender=primary_order.customer_gender,
            age=_compute_customer_current_age(primary_order),
            source=primary_order.qdly1_txt or primary_order.dzly_txt,
            notes=None,
            created_at=_parse_jdrq(primary_order) or _visit_created_at_from_order(primary_order),
        )
        db.add(customer)
        await db.flush()
    else:
        customer.name = customer_name
        customer.source = primary_order.qdly1_txt or primary_order.dzly_txt or customer.source
        if primary_order.customer_gender and not customer.gender:
            customer.gender = primary_order.customer_gender
        computed_age = _compute_customer_current_age(primary_order)
        if computed_age is not None:
            customer.age = computed_age

    staff_codes = {
        str(value or "").strip()
        for value in (primary_order.fzr_id_dq, primary_order.fzuer)
        if str(value or "").strip()
    }
    staff_by_external_code: dict[str, Staff] = {}
    if staff_codes:
        staff_rows = (
            await db.execute(select(Staff).where(Staff.external_account.in_(staff_codes)))
        ).scalars().all()
        staff_by_external_code = {staff.external_account: staff for staff in staff_rows if staff.external_account}
    consultant = (
        staff_by_external_code.get(str(primary_order.fzr_id_dq or "").strip())
        or staff_by_external_code.get(str(primary_order.fzuer or "").strip())
    )

    if visit is None:
        visit = Visit(
            customer_id=customer.id,
            external_visit_order_no=dzdh,
            external_visit_order_seg=primary_order.dzseg,
            created_at=_visit_created_at_from_order(primary_order),
        )
        db.add(visit)
        await db.flush()

    visit.customer_id = customer.id
    visit.external_visit_order_no = dzdh
    visit.external_visit_order_seg = primary_order.dzseg
    visit.consultant_id = consultant.id if consultant else visit.consultant_id
    visit.visit_date = _parse_visit_order_date(primary_order.sjrq) or _parse_visit_order_date(primary_order.crtdt)
    visit.visit_time = _format_time(primary_order.fzsj)
    visit.deal_status = primary_order.jcsta_txt
    visit.arrival_purpose = primary_order.dymd_txt
    visit.project_needs = _first_non_empty(primary_order.remark_dz)
    visit.updated_at = _visit_created_at_from_order(primary_order)

    statuses = {_visit_status_from_order(order) for order in group_orders}
    status_priority = ("closed_won", "closed_lost", "diagnosed", "consulted", "assigned", "created")
    visit.status = next((status for status in status_priority if status in statuses), visit.status)

    if len(group_orders) == 1:
        visit.notes = _build_visit_notes(primary_order)
    else:
        notes_parts: list[str] = []
        for item in group_orders:
            seg_note = _build_visit_notes(item)
            advxc_label = item.advxc_long or item.advxc or ""
            seg_header = f"[行项目 {item.dzseg}"
            if advxc_label:
                seg_header += f" | {advxc_label}"
            seg_header += "]"
            notes_parts.append(f"{seg_header} {seg_note}" if seg_note else seg_header)
        visit.notes = "\n".join(notes_parts)

    await db.flush()
    return visit


async def _find_pending_notification(
    db: AsyncSession,
    recording: Recording,
) -> VisitOrderAdvisorNotification | None:
    recording_time = _aware(recording.created_at) or _utcnow()
    lower_bound = recording_time - _AUTO_LINK_MAX_WINDOW
    upper_bound = recording_time + _AUTO_LINK_BEFORE_TOLERANCE

    match_conditions = []
    if _clean(recording.device_id):
        match_conditions.append(VisitOrderAdvisorNotification.recording_start_device_id == recording.device_id)
    if _clean(recording.staff_id):
        match_conditions.append(VisitOrderAdvisorNotification.recording_start_staff_id == recording.staff_id)
    if not match_conditions:
        return None

    return (
        await db.execute(
            select(VisitOrderAdvisorNotification)
            .where(
                VisitOrderAdvisorNotification.auto_link_status == "pending",
                VisitOrderAdvisorNotification.auto_link_recording_id.is_(None),
                VisitOrderAdvisorNotification.recording_start_requested_at.is_not(None),
                VisitOrderAdvisorNotification.recording_start_requested_at >= lower_bound,
                VisitOrderAdvisorNotification.recording_start_requested_at <= upper_bound,
                or_(*match_conditions),
            )
            .order_by(VisitOrderAdvisorNotification.recording_start_requested_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _send_auto_link_message(
    db: AsyncSession,
    notification: VisitOrderAdvisorNotification,
    recording: Recording,
    visit_order: VisitOrder,
) -> bool:
    user_id = _clean(notification.wecom_user_id)
    if not user_id:
        return False
    try:
        tenant = await _resolve_tenant_for_hospital(db, notification.hospital_code)
    except Exception as exc:
        logger.warning("resolve tenant for auto-link message failed hospital=%s: %s", notification.hospital_code, exc)
        return False

    customer_code = _clean(visit_order.kunr) or _clean(notification.customer_code) or "-"
    customer_name = _clean(visit_order.ninam) or _clean(notification.customer_name) or "-"
    content = "\n".join(
        [
            "录音已自动关联到诊单",
            f"录音：{display_recording_name(recording)}",
            f"到诊单号：{_clean(visit_order.dzdh) or notification.visit_order_no}",
            f"客户号：{customer_code}",
            f"客户姓名：{customer_name}",
        ]
    )
    await send_wecom_text_message(
        to_user=user_id,
        content=content,
        tenant=tenant,
        enable_duplicate_check=False,
    )
    return True


async def try_auto_link_visit_card_recording(db: AsyncSession, recording: Recording) -> bool:
    if recording.split_parent_recording_id:
        return False

    notification = await _find_pending_notification(db, recording)
    if notification is None:
        return False

    visit_order = await _load_visit_order_for_notification(db, notification)
    if visit_order is None:
        notification.auto_link_error_message = "未找到对应到诊单，等待后续同步后重试"
        await db.flush()
        return False

    try:
        visit = await _ensure_local_visit_for_visit_order(db, visit_order)
        existing_visit_ids = [link.visit_id for link in ordered_recording_visit_links(recording)]
        target_visit_ids = [visit.id, *existing_visit_ids]
        await sync_recording_visit_links(
            db,
            recording,
            target_visit_ids,
            primary_visit_id=visit.id,
            source="wecom_card",
        )
        now = _utcnow()
        notification.auto_link_status = "linked"
        notification.auto_link_recording_id = recording.id
        notification.auto_linked_at = now
        notification.auto_link_error_message = None
        try:
            sent = await _send_auto_link_message(db, notification, recording, visit_order)
            if sent:
                notification.auto_link_message_sent_at = _utcnow()
        except Exception as exc:
            notification.auto_link_error_message = f"已绑定，但企业微信提示发送失败：{exc}"
            logger.exception(
                "send visit-order recording auto-link message failed notification_id=%s recording_id=%s",
                notification.id,
                recording.id,
            )
        await db.flush()
        logger.info(
            "auto-linked recording from visit-order card notification_id=%s recording_id=%s visit_id=%s",
            notification.id,
            recording.id,
            visit.id,
        )
        return True
    except Exception as exc:
        notification.auto_link_error_message = str(exc)
        await db.flush()
        logger.exception(
            "auto-link recording from visit-order card failed notification_id=%s recording_id=%s",
            notification.id,
            recording.id,
        )
        return False
