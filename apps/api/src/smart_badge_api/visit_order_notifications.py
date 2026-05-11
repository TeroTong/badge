from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.customer_type import normalize_customer_type_code, normalize_customer_type_label
from smart_badge_api.db.models import SapHanaVisitOrder, Staff, User, VisitOrderAdvisorNotification, WecomTenant
from smart_badge_api.schemas.visit_order_push import SapHanaVisitOrderPushIn
from smart_badge_api.wecom import (
    WecomApiError,
    WecomConfigError,
    send_wecom_button_interaction_card,
    send_wecom_textcard_message,
)
from smart_badge_api.wecom_tenants import resolve_wecom_tenant_config

logger = logging.getLogger("smart_badge.visit_order_notifications")

_RECORDING_ACTION_PREFIX = "visit_order_recording"
_TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")
_NON_ARRIVAL_PURPOSE_CODES = {"X", "Z"}
_NON_ARRIVAL_PURPOSE_LABELS = {"\u672a\u5230\u9662\u8d2d\u4e70", "\u5176\u4ed6"}
_ARRIVAL_PURPOSE_LABELS = {
    "A": "咨询",
    "B": "治疗",
    "C": "手术",
    "D": "复查",
}


def _safe_task_component(value: object, *, fallback: str = "manual", max_length: int = 80) -> str:
    text = str(value or "").strip() or fallback
    safe = "".join(ch if ch.isalnum() or ch in "_-@" else "_" for ch in text)
    return (safe or fallback)[:max_length]


def build_recording_action_key(action: str, log_id: str | None) -> str:
    return f"{_RECORDING_ACTION_PREFIX}__{_safe_task_component(action)}__{_safe_task_component(log_id)}"


def parse_recording_action_key(value: str | None) -> tuple[str, str] | None:
    text = str(value or "").strip()
    prefix = f"{_RECORDING_ACTION_PREFIX}__"
    if not text.startswith(prefix):
        return None
    parts = text.split("__", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def build_recording_card_task_id(log_id: str | None, *, action: str = "start") -> str:
    return f"vor_{_safe_task_component(log_id)}_{_safe_task_component(action, max_length=20)}"


def _build_card_url(*, frontend_url: str, visit_order_no: str) -> str:
    target_path = f"/wecom/badge?{urlencode({'action': 'start', 'visit_order_no': visit_order_no})}"
    return f"{frontend_url.rstrip('/')}/login?wecom=1&redirect={quote(target_path, safe='')}"


@dataclass(slots=True)
class VisitOrderAdvisorCandidate:
    hospital_code: str
    visit_order_no: str
    visit_order_seg: str
    triage_no: str | None
    advisor_code: str | None
    advisor_name: str | None
    customer_code: str | None
    customer_name: str | None
    customer_type_code: str | None
    customer_type_label: str | None
    visit_date: str | None
    visit_time: str | None
    department_code: str | None
    arrival_purpose: str | None
    demand: str | None


def _clean_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _compact_text(value: object) -> str:
    return "".join(str(value or "").split())


def _is_non_arrival_purpose(*values: object) -> bool:
    for value in values:
        text = _clean_text(value)
        if not text:
            continue
        if text.upper() in _NON_ARRIVAL_PURPOSE_CODES:
            return True
        if _compact_text(text) in _NON_ARRIVAL_PURPOSE_LABELS:
            return True
    return False


def _extract_arrival_purpose_label(serialized: dict[str, Any]) -> str | None:
    for key in ("DYMD_TXT", "dymd_txt", "DYMDTXT", "DYMD_TEXT"):
        text = _clean_text(serialized.get(key))
        if text:
            return text
    code = _clean_text(serialized.get("DYMD"))
    if code:
        return _ARRIVAL_PURPOSE_LABELS.get(code.upper())
    return None


def _format_card_push_time(value: datetime) -> str:
    aware_value = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware_value.astimezone(_TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _format_card_push_time_short(value: datetime) -> str:
    aware_value = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware_value.astimezone(_TZ_SHANGHAI).strftime("%m-%d %H:%M")


def _normalize_sap_date_token(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 8:
        try:
            return datetime.strptime(digits, "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    if len(text) >= 10:
        try:
            return datetime.fromisoformat(text[:10]).date().isoformat()
        except ValueError:
            return None
    return None


def _normalize_sap_time_token(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 6:
        return f"{digits[:2]}:{digits[2:4]}:{digits[4:6]}"
    if len(digits) == 4:
        return f"{digits[:2]}:{digits[2:4]}"
    return text


def _derive_visit_order_segment(dzdh: str, fzdh: str | None, index: int) -> str:
    triage_no = _clean_text(fzdh)
    if triage_no:
        if triage_no.startswith(f"{dzdh}-"):
            suffix = triage_no[len(dzdh) + 1 :].strip()
            if suffix:
                return suffix[:9]
        if "-" in triage_no:
            suffix = triage_no.rsplit("-", 1)[-1].strip()
            if suffix:
                return suffix[:9]
    return f"{index:03d}"


def _iter_triage_rows(serialized: dict[str, Any]) -> list[dict[str, Any]]:
    rows = serialized.get("FZDATA")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def _extract_candidates(payloads: list[SapHanaVisitOrderPushIn]) -> list[VisitOrderAdvisorCandidate]:
    result: list[VisitOrderAdvisorCandidate] = []
    seen: set[tuple[str, str, str, str | None]] = set()

    for payload in payloads:
        serialized = payload.model_dump(by_alias=True)
        hospital_code = _clean_text(serialized.get("JGBM"))
        visit_order_no = _clean_text(serialized.get("DZDH"))
        if not hospital_code or not visit_order_no:
            continue
        arrival_purpose = _clean_text(serialized.get("DYMD"))
        arrival_purpose_label = _extract_arrival_purpose_label(serialized)
        if _is_non_arrival_purpose(arrival_purpose, arrival_purpose_label):
            continue

        triage_rows = _iter_triage_rows(serialized)
        if not triage_rows:
            triage_rows = [serialized]

        for index, triage in enumerate(triage_rows, start=1):
            advisor_code = _clean_text(triage.get("ADVXC"))
            advisor_name = _clean_text(triage.get("ADVXC_LONG"))
            if not advisor_code and not advisor_name:
                continue

            triage_no = _clean_text(triage.get("FZDH"))
            visit_order_seg = _derive_visit_order_segment(visit_order_no, triage_no, index)
            key = (hospital_code, visit_order_no, visit_order_seg, advisor_code or advisor_name)
            if key in seen:
                continue
            seen.add(key)
            result.append(
                VisitOrderAdvisorCandidate(
                    hospital_code=hospital_code,
                    visit_order_no=visit_order_no,
                    visit_order_seg=visit_order_seg,
                    triage_no=triage_no,
                    advisor_code=advisor_code,
                    advisor_name=advisor_name,
                    customer_code=_clean_text(serialized.get("KUNR")),
                    customer_name=_clean_text(serialized.get("NINAM")),
                    customer_type_code=normalize_customer_type_code(serialized.get("KUT30_DQ")),
                    customer_type_label=normalize_customer_type_label(
                        serialized.get("KUT30_DQ"),
                        serialized.get("KUT30_DQ_TXT") or serialized.get("KHLX_T30"),
                    ),
                    visit_date=_normalize_sap_date_token(serialized.get("CRTDT")),
                    visit_time=_normalize_sap_time_token(triage.get("FZSJ")) or _normalize_sap_time_token(serialized.get("CRTTM")),
                    department_code=_clean_text(serialized.get("JGKS")),
                    arrival_purpose=arrival_purpose_label,
                    demand=_clean_text(serialized.get("REMARK_DZ")),
                )
            )

    return result


def _hospital_matches(staff: Staff, hospital_code: str) -> bool:
    staff_hospital_code = _clean_text(staff.hospital_code)
    return not staff_hospital_code or staff_hospital_code == hospital_code


def _choose_unique_staff(rows: list[Staff], *, hospital_code: str, advisor_name: str | None) -> Staff | None:
    if not rows:
        return None
    hospital_rows = [row for row in rows if _hospital_matches(row, hospital_code)]
    rows = hospital_rows or rows
    if advisor_name:
        named_rows = [row for row in rows if _clean_text(row.name) == advisor_name]
        if named_rows:
            rows = named_rows
    return rows[0] if len(rows) == 1 else None


async def _load_advisor_staff(db: AsyncSession, candidate: VisitOrderAdvisorCandidate) -> Staff | None:
    if candidate.advisor_code:
        rows = (
            await db.execute(
                select(Staff).where(
                    Staff.is_active.is_(True),
                    or_(
                        Staff.external_account == candidate.advisor_code,
                        Staff.wecom_user_id == candidate.advisor_code,
                        Staff.badge_id == candidate.advisor_code,
                    ),
                )
            )
        ).scalars().all()
        staff = _choose_unique_staff(rows, hospital_code=candidate.hospital_code, advisor_name=candidate.advisor_name)
        if staff is not None:
            return staff

    if candidate.advisor_name:
        rows = (
            await db.execute(
                select(Staff).where(
                    Staff.is_active.is_(True),
                    Staff.name == candidate.advisor_name,
                )
            )
        ).scalars().all()
        return _choose_unique_staff(rows, hospital_code=candidate.hospital_code, advisor_name=candidate.advisor_name)

    return None


async def _load_active_user_for_staff(db: AsyncSession, staff: Staff) -> User | None:
    conditions = [User.staff_id == staff.id]
    for value in (staff.external_account, staff.phone, staff.wecom_user_id):
        text = _clean_text(value)
        if text:
            conditions.append(User.username == text)
    return (
        await db.execute(
            select(User)
            .where(User.is_active.is_(True), or_(*conditions))
            .order_by(User.staff_id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _load_notification_log(
    db: AsyncSession,
    *,
    candidate: VisitOrderAdvisorCandidate,
    staff: Staff,
) -> VisitOrderAdvisorNotification:
    log = (
        await db.execute(
            select(VisitOrderAdvisorNotification)
            .where(
                VisitOrderAdvisorNotification.hospital_code == candidate.hospital_code,
                VisitOrderAdvisorNotification.visit_order_no == candidate.visit_order_no,
                VisitOrderAdvisorNotification.visit_order_seg == candidate.visit_order_seg,
                VisitOrderAdvisorNotification.advisor_staff_id == staff.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if log is not None:
        return log

    log = VisitOrderAdvisorNotification(
        hospital_code=candidate.hospital_code,
        visit_order_no=candidate.visit_order_no,
        visit_order_seg=candidate.visit_order_seg,
        triage_no=candidate.triage_no,
        advisor_code=candidate.advisor_code,
        advisor_name=candidate.advisor_name or staff.name,
        advisor_staff_id=staff.id,
        wecom_user_id=_clean_text(staff.wecom_user_id),
        customer_code=candidate.customer_code,
        customer_name=candidate.customer_name,
        status="pending",
    )
    db.add(log)
    await db.commit()
    await db.refresh(log)
    return log


async def _resolve_tenant_for_hospital(db: AsyncSession, hospital_code: str):
    tenant_id = (
        await db.execute(
            select(WecomTenant.id)
            .where(
                WecomTenant.is_active.is_(True),
                WecomTenant.default_hospital_code == hospital_code,
                WecomTenant.corp_id.is_not(None),
                WecomTenant.corp_id != "",
                WecomTenant.agent_id.is_not(None),
                WecomTenant.agent_id != "",
                WecomTenant.agent_secret.is_not(None),
                WecomTenant.agent_secret != "",
                WecomTenant.frontend_url.is_not(None),
                WecomTenant.frontend_url != "",
            )
            .order_by(WecomTenant.is_default.desc(), WecomTenant.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if tenant_id:
        return await resolve_wecom_tenant_config(db, tenant_id=tenant_id)
    return await resolve_wecom_tenant_config(db)


def _build_card_customer_text(candidate: VisitOrderAdvisorCandidate) -> str:
    customer = candidate.customer_name or "未填写客户"
    if candidate.customer_code:
        customer = f"{customer}（{candidate.customer_code}）"
    return customer


def _build_card_title(candidate: VisitOrderAdvisorCandidate) -> str:
    return f"{_build_card_customer_text(candidate)}｜{candidate.customer_type_label or '未标识'}"


def _build_card_description(candidate: VisitOrderAdvisorCandidate, staff: Staff, *, pushed_at: datetime) -> str:
    return "\n".join(
        [
            "请确认工牌在线后点击开始录音。",
            "录音完成上传后，系统会自动关联该到诊单。",
        ]
    )


def _build_card_horizontal_items(candidate: VisitOrderAdvisorCandidate, *, pushed_at: datetime) -> list[dict[str, str]]:
    order_display = f"{candidate.visit_order_no}-{candidate.visit_order_seg}"
    return [
        {"keyname": "客户姓名", "value": (candidate.customer_name or "未填写")[:32]},
        {"keyname": "客户编号", "value": (candidate.customer_code or "未填写")[:32]},
        {"keyname": "新老客标识", "value": (candidate.customer_type_label or "未标识")[:32]},
        {"keyname": "到诊单号", "value": order_display[:32]},
        {"keyname": "到院目的", "value": (candidate.arrival_purpose or "未填写")[:32]},
        {"keyname": "卡片推送时间", "value": _format_card_push_time(pushed_at)},
    ]


async def notify_pushed_visit_order_advisors(
    db: AsyncSession,
    payloads: list[SapHanaVisitOrderPushIn],
) -> int:
    sent_count = 0
    for candidate in _extract_candidates(payloads):
        staff = await _load_advisor_staff(db, candidate)
        if staff is None:
            continue

        user = await _load_active_user_for_staff(db, staff)
        if user is None:
            continue

        log = await _load_notification_log(db, candidate=candidate, staff=staff)
        if log.status == "sent" and log.wecom_task_id:
            continue

        wecom_user_id = _clean_text(staff.wecom_user_id)
        if not wecom_user_id:
            log.status = "skipped"
            log.error_message = "接诊人账号未绑定企业微信 UserId"
            await db.commit()
            continue

        try:
            tenant = await _resolve_tenant_for_hospital(db, candidate.hospital_code)
            pushed_at = datetime.now(timezone.utc)
            task_id = build_recording_card_task_id(log.id, action=str(int(pushed_at.timestamp())))
            try:
                response = await send_wecom_button_interaction_card(
                    to_user=wecom_user_id,
                    title=_build_card_title(candidate),
                    description=_build_card_description(candidate, staff, pushed_at=pushed_at),
                    main_title_desc=None,
                    horizontal_content_list=_build_card_horizontal_items(candidate, pushed_at=pushed_at),
                    task_id=task_id,
                    buttons=[
                        {
                            "text": "开始录音",
                            "key": build_recording_action_key("start", log.id),
                            "style": 1,
                        },
                        {
                            "text": "停止录音",
                            "key": build_recording_action_key("stop", log.id),
                            "style": 2,
                        },
                    ],
                    tenant=tenant,
                )
            except WecomApiError as exc:
                if exc.errcode != 43012:
                    raise
                response = await send_wecom_textcard_message(
                    to_user=wecom_user_id,
                    title=_build_card_title(candidate),
                    description=_build_card_description(candidate, staff, pushed_at=pushed_at).replace("\n", "<br/>"),
                    url=_build_card_url(frontend_url=tenant.frontend_url, visit_order_no=candidate.visit_order_no),
                    btn_text="开始录音",
                    tenant=tenant,
                )
                task_id = "textcard_fallback"
        except (WecomApiError, WecomConfigError) as exc:
            log.status = "failed"
            log.error_message = str(exc)
            await db.commit()
            continue
        except Exception as exc:  # pragma: no cover - external notification guard
            logger.exception(
                "visit order advisor notification failed hospital=%s dzdh=%s seg=%s staff=%s",
                candidate.hospital_code,
                candidate.visit_order_no,
                candidate.visit_order_seg,
                staff.id,
            )
            log.status = "failed"
            log.error_message = f"{type(exc).__name__}: {exc}"
            await db.commit()
            continue

        log.status = "sent"
        log.error_message = None
        log.wecom_user_id = wecom_user_id
        log.wecom_task_id = task_id
        log.wecom_response_code = _clean_text(response.get("response_code") or response.get("responseCode"))
        log.sent_at = pushed_at
        await db.commit()
        sent_count += 1

    return sent_count


async def notify_pushed_visit_order_advisors_for_keys(
    db: AsyncSession,
    keys: set[tuple[str, str]],
) -> int:
    normalized_keys = {
        (str(jgbm or "").strip(), str(dzdh or "").strip())
        for jgbm, dzdh in keys
        if str(jgbm or "").strip() and str(dzdh or "").strip()
    }
    if not normalized_keys:
        return 0

    rows = (
        await db.execute(
            select(SapHanaVisitOrder).where(
                tuple_(SapHanaVisitOrder.jgbm, SapHanaVisitOrder.dzdh).in_(normalized_keys)
            )
        )
    ).scalars().all()
    payloads: list[SapHanaVisitOrderPushIn] = []
    for row in rows:
        try:
            payloads.append(SapHanaVisitOrderPushIn.model_validate(row.source_payload))
        except Exception:
            logger.exception(
                "invalid SAP HANA visit order source payload for advisor notification hospital=%s dzdh=%s",
                row.jgbm,
                row.dzdh,
            )
    if not payloads:
        return 0
    return await notify_pushed_visit_order_advisors(db, payloads)
