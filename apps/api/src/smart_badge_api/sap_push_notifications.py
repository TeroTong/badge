from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import Recording, SapPushLog, Staff, VisitOrder
from smart_badge_api.message_push import (
    MessagePushApiError,
    MessagePushConfigError,
    resolve_message_push_auth_code,
    send_message_push,
)

logger = logging.getLogger("smart_badge.sap_push.notifications")


def _clean_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _visit_ref(no: object, seg: object = None) -> str:
    visit_no = _clean_text(no)
    if not visit_no:
        return "-"
    visit_seg = _clean_text(seg)
    return f"{visit_no}-{visit_seg}" if visit_seg else visit_no


def _short_text(value: object, *, limit: int = 180) -> str:
    text = _clean_text(value) or "无"
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def _payload_zxxx(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    zxxx = payload.get("zxxx")
    return zxxx if isinstance(zxxx, dict) else {}


def _first_payload_zxxx(push_log: SapPushLog) -> dict[str, Any]:
    payloads = list(push_log.request_payloads or [])
    return _payload_zxxx(payloads[0]) if payloads else {}


def _same_visit_seg_condition(value: str | None):
    if value:
        return SapPushLog.visit_order_seg == value
    return or_(SapPushLog.visit_order_seg.is_(None), SapPushLog.visit_order_seg == "")


def _same_push_scope_condition(push_log: SapPushLog):
    conditions = []
    recording_id = _clean_text(push_log.recording_id)
    visit_id = _clean_text(push_log.visit_id)
    visit_order_no = _clean_text(push_log.visit_order_no)
    visit_order_seg = _clean_text(push_log.visit_order_seg)

    if recording_id and visit_id:
        conditions.append(
            and_(
                SapPushLog.recording_id == recording_id,
                SapPushLog.visit_id == visit_id,
            )
        )
    if recording_id and visit_order_no:
        conditions.append(
            and_(
                SapPushLog.recording_id == recording_id,
                SapPushLog.visit_order_no == visit_order_no,
                _same_visit_seg_condition(visit_order_seg),
            )
        )
    if visit_id:
        conditions.append(SapPushLog.visit_id == visit_id)
    if visit_order_no:
        conditions.append(
            and_(
                SapPushLog.visit_order_no == visit_order_no,
                _same_visit_seg_condition(visit_order_seg),
            )
        )

    return or_(*conditions) if conditions else None


async def _has_prior_failure_notification(db: AsyncSession, push_log: SapPushLog) -> bool:
    scope_condition = _same_push_scope_condition(push_log)
    if scope_condition is None:
        return False
    existing_id = (
        await db.execute(
            select(SapPushLog.id)
            .where(
                SapPushLog.id != push_log.id,
                SapPushLog.message_failure_notified_at.is_not(None),
                scope_condition,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return existing_id is not None


async def _load_recording_staff(db: AsyncSession, push_log: SapPushLog) -> tuple[Recording | None, Staff | None]:
    recording_id = _clean_text(push_log.recording_id)
    if not recording_id:
        return None, None
    recording = await db.get(Recording, recording_id)
    if recording is None or not recording.staff_id:
        return recording, None
    return recording, await db.get(Staff, recording.staff_id)


async def _load_visit_order(db: AsyncSession, push_log: SapPushLog) -> VisitOrder | None:
    visit_order_no = _clean_text(push_log.visit_order_no)
    if not visit_order_no:
        zxxx = _first_payload_zxxx(push_log)
        fzdh = _clean_text(zxxx.get("fzdh"))
        if fzdh and "-" in fzdh:
            visit_order_no, visit_order_seg = fzdh.rsplit("-", 1)
        else:
            visit_order_no, visit_order_seg = fzdh, None
    else:
        visit_order_seg = _clean_text(push_log.visit_order_seg)

    if not visit_order_no:
        return None

    stmt = select(VisitOrder).where(VisitOrder.dzdh == visit_order_no)
    if visit_order_seg:
        stmt = stmt.where(VisitOrder.dzseg == visit_order_seg)
    else:
        stmt = stmt.where(or_(VisitOrder.dzseg.is_(None), VisitOrder.dzseg == ""))
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


async def _load_staff_by_code(
    db: AsyncSession,
    *,
    staff_code: str | None,
    hospital_code: str | None,
) -> Staff | None:
    code = _clean_text(staff_code)
    if not code:
        return None
    stmt = select(Staff).where(Staff.external_account == code, Staff.is_active.is_(True))
    if hospital_code:
        stmt = stmt.where(Staff.hospital_code == hospital_code)
    staff = (await db.execute(stmt.order_by(Staff.updated_at.desc()).limit(1))).scalar_one_or_none()
    if staff is not None or not hospital_code:
        return staff
    return (
        await db.execute(
            select(Staff)
            .where(Staff.external_account == code, Staff.is_active.is_(True))
            .order_by(Staff.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _dedupe(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _clean_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _pick_advisor_code(visit_order: VisitOrder | None, recording_staff: Staff | None) -> str | None:
    codes = _dedupe(
        [
            getattr(visit_order, "advxc", None),
            getattr(visit_order, "fzuer", None),
            getattr(visit_order, "fzr_id_dq", None),
            getattr(visit_order, "d_fzuer", None),
            getattr(recording_staff, "external_account", None),
        ]
    )
    return codes[0] if codes else None


def _resolve_hospital_code(
    *,
    visit_order: VisitOrder | None,
    recording_staff: Staff | None,
    target_staff: Staff | None,
    push_log: SapPushLog,
) -> str | None:
    zxxx = _first_payload_zxxx(push_log)
    return (
        _clean_text(getattr(visit_order, "jgbm", None))
        or _clean_text(getattr(target_staff, "hospital_code", None))
        or _clean_text(getattr(recording_staff, "hospital_code", None))
        or _clean_text(zxxx.get("JGBM"))
    )


def _resolve_advisor_name(
    *,
    visit_order: VisitOrder | None,
    target_staff: Staff | None,
    recording_staff: Staff | None,
    push_log: SapPushLog,
) -> str:
    return (
        _clean_text(getattr(visit_order, "advxc_long", None))
        or _clean_text(getattr(visit_order, "fzuer_long", None))
        or _clean_text(getattr(target_staff, "name", None))
        or _clean_text(push_log.advisor_name)
        or _clean_text(getattr(recording_staff, "name", None))
        or "咨询师"
    )


def _build_message_content(
    *,
    push_log: SapPushLog,
    recording: Recording | None,
    visit_order: VisitOrder | None,
    advisor_name: str,
    status: str,
    had_prior_failure: bool,
) -> tuple[str, str]:
    visit_order_ref = _visit_ref(
        getattr(visit_order, "dzdh", None) or push_log.visit_order_no,
        getattr(visit_order, "dzseg", None) or push_log.visit_order_seg,
    )
    customer_name = (
        _clean_text(getattr(visit_order, "ninam", None))
        or _clean_text(push_log.customer_name)
        or "-"
    )
    recording_name = _clean_text(getattr(recording, "file_name", None)) or "-"
    reason = _short_text(push_log.business_message or push_log.error_message or push_log.status, limit=220)

    if status == "succeeded":
        title = "SAP咨询单回传成功"
        prefix = "此前失败的 SAP 咨询单重试已成功回传。" if had_prior_failure else "SAP 咨询单已回传成功。"
        content = (
            f"{advisor_name}，{prefix}\n"
            f"到诊单：{visit_order_ref}\n"
            f"客户：{customer_name}\n"
            f"录音：{recording_name}"
        )
        return title, content

    title = "SAP咨询单回传失败"
    content = (
        f"{advisor_name}，这条 SAP 咨询单回传失败，系统会按规则自动重试。\n"
        f"到诊单：{visit_order_ref}\n"
        f"客户：{customer_name}\n"
        f"录音：{recording_name}\n"
        f"失败原因：{reason}"
    )
    return title, content


async def notify_sap_push_result(db: AsyncSession, push_log: SapPushLog) -> bool:
    settings = get_settings()
    if not settings.message_push_sap_result_enabled:
        return False

    status = _clean_text(push_log.status)
    if status not in {"succeeded", "failed"}:
        return False

    had_prior_failure = await _has_prior_failure_notification(db, push_log)
    if status == "failed":
        if push_log.message_failure_notified_at or had_prior_failure:
            return False
    elif push_log.message_success_notified_at:
        return False

    recording, recording_staff = await _load_recording_staff(db, push_log)
    visit_order = await _load_visit_order(db, push_log)
    advisor_code = _pick_advisor_code(visit_order, recording_staff)
    hospital_code = _resolve_hospital_code(
        visit_order=visit_order,
        recording_staff=recording_staff,
        target_staff=None,
        push_log=push_log,
    )
    target_staff = await _load_staff_by_code(db, staff_code=advisor_code, hospital_code=hospital_code)
    hospital_code = _resolve_hospital_code(
        visit_order=visit_order,
        recording_staff=recording_staff,
        target_staff=target_staff,
        push_log=push_log,
    )
    advisor_name = _resolve_advisor_name(
        visit_order=visit_order,
        target_staff=target_staff,
        recording_staff=recording_staff,
        push_log=push_log,
    )

    auth_code = resolve_message_push_auth_code(hospital_code)
    if not auth_code:
        push_log.message_notify_error = f"未找到机构 {hospital_code or '-'} 的消息平台 Auth Code"
        await db.commit()
        return False

    target_code = _clean_text(advisor_code) or _clean_text(getattr(target_staff, "external_account", None))
    if not target_code:
        push_log.message_notify_error = "未找到咨询师员工编号，无法发送 SAP 回传结果提醒"
        await db.commit()
        return False

    title, content = _build_message_content(
        push_log=push_log,
        recording=recording,
        visit_order=visit_order,
        advisor_name=advisor_name,
        status=status,
        had_prior_failure=had_prior_failure,
    )

    try:
        await send_message_push(
            title=title,
            content=content,
            auth_code=auth_code,
            targets=[target_code],
            biz_user_id=settings.message_push_sap_result_biz_user_id,
            org_code=hospital_code,
            msg_type="text",
        )
    except (MessagePushApiError, MessagePushConfigError) as exc:
        push_log.message_notify_error = str(exc)
        await db.commit()
        return False
    except Exception as exc:  # pragma: no cover - defensive guard around external platform
        logger.exception("sap push message notification failed log_id=%s", push_log.id)
        push_log.message_notify_error = f"{type(exc).__name__}: {exc}"
        await db.commit()
        return False

    now = _utcnow()
    if status == "succeeded":
        push_log.message_success_notified_at = now
    else:
        push_log.message_failure_notified_at = now
    push_log.message_notify_error = None
    await db.commit()
    return True
