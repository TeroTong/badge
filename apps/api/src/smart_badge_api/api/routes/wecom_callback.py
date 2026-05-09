from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.audit import append_audit_log
from smart_badge_api.api.routes.account import _handle_dingtalk_error, _require_my_badge_recording_context
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import Staff, User, VisitOrder, VisitOrderAdvisorNotification, WecomTenant
from smart_badge_api.db.session import get_db
from smart_badge_api.dingtalk import DingTalkApiError, DingTalkConfigError, dvi_control_recording
from smart_badge_api.dingtalk_iot import iot_control_recording
from smart_badge_api.visit_order_notifications import (
    build_recording_action_key,
    build_recording_card_task_id,
    parse_recording_action_key,
)
from smart_badge_api.visit_order_card_recording_link import mark_visit_order_card_recording_started
from smart_badge_api.wecom import (
    WecomTenantConfig,
    legacy_wecom_tenant_config,
    send_wecom_text_message,
    update_wecom_button_interaction_card,
    update_wecom_template_card_button,
)
from smart_badge_api.wecom_callback_crypto import (
    WecomCallbackCryptoError,
    decrypt_callback_payload,
    parse_xml_flat_texts,
)
from smart_badge_api.wecom_tenants import resolve_wecom_tenant_config

router = APIRouter(prefix="/wecom/callback", tags=["企业微信回调"])
logger = logging.getLogger("smart_badge.wecom_callback")
_ACTION_KEY_PATTERN = re.compile(r"visit_order_recording__(?:start|stop|confirm|cancel|retry|done)__[A-Za-z0-9_.@-]+")
_TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(slots=True)
class _CallbackContext:
    tenant: WecomTenantConfig
    token: str
    aes_key: str


def _clean(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _mask(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}***{text[-2:]}"


def _field(values: dict[str, str], *names: str) -> str | None:
    lowered = {key.lower(): value for key, value in values.items()}
    for name in names:
        value = values.get(name)
        if value:
            return value
        value = lowered.get(name.lower())
        if value:
            return value
        target = name.lower()
        for key, candidate in values.items():
            leaf = key.rsplit("/", 1)[-1].lower()
            if candidate and leaf == target:
                return candidate
    return None


def _field_summary(values: dict[str, str]) -> dict[str, str]:
    summary: dict[str, str] = {}
    for name in ("MsgType", "Event", "EventKey", "TaskId", "TaskID", "ResponseCode", "CardType"):
        value = _field(values, name)
        if value:
            summary[name] = value
    return summary


def _customer_type_label(value: object) -> str | None:
    text = _clean(value)
    if not text:
        return None
    normalized = text.upper()
    if normalized == "Q":
        return "新客"
    if normalized == "V":
        return "老客"
    return text


def _format_card_push_time(value: object) -> str:
    if not isinstance(value, datetime):
        return "-"
    aware_value = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware_value.astimezone(_TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


async def _load_card_visit_context(
    db: AsyncSession,
    log_id: str | None,
) -> tuple[str, str | None, list[dict[str, str | int]]]:
    normalized_log_id = _clean(log_id)
    if not normalized_log_id or normalized_log_id == "sample":
        return "接诊客户", None, []

    notification = await db.get(VisitOrderAdvisorNotification, normalized_log_id)
    if notification is None:
        return "接诊客户", None, []

    order_stmt = select(VisitOrder).where(VisitOrder.dzdh == notification.visit_order_no)
    if _clean(notification.hospital_code):
        order_stmt = order_stmt.where(VisitOrder.jgbm == notification.hospital_code)
    if _clean(notification.visit_order_seg):
        order = (
            await db.execute(order_stmt.where(VisitOrder.dzseg == notification.visit_order_seg).limit(1))
        ).scalar_one_or_none()
    else:
        order = None
    if order is None:
        order = (
            await db.execute(order_stmt.order_by(VisitOrder.dzseg.asc(), VisitOrder.id.asc()).limit(1))
        ).scalar_one_or_none()

    visit_order_no = _clean(getattr(order, "dzdh", None)) or _clean(notification.visit_order_no) or "-"
    visit_order_seg = _clean(getattr(order, "dzseg", None)) or _clean(notification.visit_order_seg)
    customer_name = _clean(getattr(order, "ninam", None)) or _clean(notification.customer_name) or "-"
    customer_code = _clean(getattr(order, "kunr", None)) or _clean(notification.customer_code) or "-"
    push_time = _format_card_push_time(notification.sent_at or notification.created_at)
    customer_type = (
        _customer_type_label(getattr(order, "kut30_dq_txt", None))
        or _customer_type_label(getattr(order, "khlx_t30", None))
        or _customer_type_label(getattr(order, "kut30_dq", None))
        or "未标识"
    )

    title = f"{customer_name}｜{customer_type}"
    order_display = visit_order_no if not visit_order_seg else f"{visit_order_no}-{visit_order_seg}"
    subtitle = f"到诊单：{order_display}"
    horizontal_items: list[dict[str, str | int]] = [
        {"keyname": "到诊单号", "value": order_display},
        {"keyname": "客户姓名", "value": customer_name},
        {"keyname": "客户编号", "value": customer_code},
        {"keyname": "卡片推送时间", "value": push_time},
        {"keyname": "新老客", "value": customer_type},
    ]
    return title, subtitle, horizontal_items


def _parse_recording_task_id(value: str | None) -> tuple[str, str] | None:
    text = str(value or "").strip()
    if not text.startswith("vor_"):
        return None
    parts = text[4:].split("_", 1)
    if not parts or not parts[0]:
        return None
    suffix = parts[1] if len(parts) > 1 else ""
    action = next(
        (candidate for candidate in ("confirm", "stop", "retry", "cancel", "done", "start") if suffix.startswith(candidate)),
        "start",
    )
    return action, parts[0]


def _find_recording_action(values: dict[str, str]) -> tuple[str, str, str]:
    event_key = _field(values, "EventKey", "Event_Key", "Key", "key")
    parsed = parse_recording_action_key(event_key)
    if parsed is not None:
        return parsed[0], parsed[1], "event_key"

    for value in values.values():
        match = _ACTION_KEY_PATTERN.search(str(value or ""))
        if not match:
            continue
        parsed = parse_recording_action_key(match.group(0))
        if parsed is not None:
            return parsed[0], parsed[1], "payload_search"

    task_id = _field(values, "TaskId", "TaskID", "task_id", "taskid")
    parsed = _parse_recording_task_id(task_id)
    if parsed is not None:
        return parsed[0], parsed[1], "task_id"
    return "", "", ""


def _legacy_callback_context() -> _CallbackContext | None:
    settings = get_settings()
    token = _clean(settings.wecom_callback_token)
    aes_key = _clean(settings.wecom_callback_aes_key)
    tenant = legacy_wecom_tenant_config()
    if tenant is None or not token or not aes_key:
        return None
    return _CallbackContext(tenant=tenant, token=token, aes_key=aes_key)


async def _row_to_callback_context(db: AsyncSession, row: WecomTenant) -> _CallbackContext:
    tenant = await resolve_wecom_tenant_config(db, tenant_id=row.id)
    token = _clean(row.callback_token) or _clean(get_settings().wecom_callback_token)
    aes_key = _clean(row.callback_aes_key) or _clean(get_settings().wecom_callback_aes_key)
    if not token or not aes_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "企业微信回调 Token 或 EncodingAESKey 未配置")
    return _CallbackContext(tenant=tenant, token=token, aes_key=aes_key)


async def _resolve_callback_context(db: AsyncSession, tenant_id: str | None) -> _CallbackContext:
    normalized_tenant_id = _clean(tenant_id)
    if normalized_tenant_id:
        row = (
            await db.execute(
                select(WecomTenant)
                .where(
                    WecomTenant.is_active.is_(True),
                    or_(WecomTenant.id == normalized_tenant_id, WecomTenant.corp_id == normalized_tenant_id),
                )
                .order_by(WecomTenant.is_default.desc(), WecomTenant.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            return await _row_to_callback_context(db, row)
        legacy = _legacy_callback_context()
        if legacy is not None and normalized_tenant_id in {legacy.tenant.id, legacy.tenant.corp_id}:
            return legacy
        raise HTTPException(status.HTTP_404_NOT_FOUND, "企业微信回调主体不存在")

    rows = (
        await db.execute(
            select(WecomTenant)
            .where(
                WecomTenant.is_active.is_(True),
                WecomTenant.callback_token.is_not(None),
                WecomTenant.callback_token != "",
                WecomTenant.callback_aes_key.is_not(None),
                WecomTenant.callback_aes_key != "",
            )
            .order_by(WecomTenant.is_default.desc(), WecomTenant.updated_at.desc())
            .limit(2)
        )
    ).scalars().all()
    if len(rows) == 1:
        return await _row_to_callback_context(db, rows[0])
    if len(rows) > 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "存在多个企业微信回调主体，请在回调 URL 上带 tenant_id")

    legacy = _legacy_callback_context()
    if legacy is not None:
        return legacy
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "企业微信回调配置未完成")


async def _load_user_by_wecom_userid(db: AsyncSession, *, userid: str, corp_id: str) -> tuple[Staff | None, User | None]:
    staff_stmt = select(Staff).where(Staff.is_active.is_(True), Staff.wecom_user_id == userid)
    if corp_id:
        staff_stmt = staff_stmt.where(
            or_(Staff.wecom_corp_id == corp_id, Staff.wecom_corp_id.is_(None), Staff.wecom_corp_id == "")
        )
    staff = (await db.execute(staff_stmt.order_by(Staff.updated_at.desc()).limit(1))).scalar_one_or_none()

    conditions = [User.username == userid]
    if staff is not None:
        conditions.insert(0, User.staff_id == staff.id)
        for value in (staff.external_account, staff.phone, staff.wecom_user_id):
            text = _clean(value)
            if text:
                conditions.append(User.username == text)
    user = (
        await db.execute(
            select(User)
            .where(User.is_active.is_(True), or_(*conditions))
            .order_by(User.staff_id.desc(), User.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return staff, user


async def _remember_response_code(db: AsyncSession, *, log_id: str, response_code: str | None) -> None:
    if not log_id or not response_code:
        return
    log = await db.get(VisitOrderAdvisorNotification, log_id)
    if log is None:
        return
    log.wecom_response_code = response_code
    await db.commit()


async def _card_recording_already_started(db: AsyncSession, log_id: str) -> bool:
    if not _clean(log_id) or log_id == "sample":
        return False
    log = await db.get(VisitOrderAdvisorNotification, log_id)
    if log is None:
        return False
    return bool(
        log.recording_start_requested_at
        or log.auto_link_recording_id
        or _clean(log.auto_link_status) in {"pending", "linked"}
    )


async def _send_card_button_feedback(
    tenant: WecomTenantConfig,
    userid: str,
    response_code: str | None,
    replace_name: str,
) -> None:
    clean_response_code = _clean(response_code)
    if not clean_response_code:
        return
    try:
        response = await update_wecom_template_card_button(
            to_user=userid,
            response_code=clean_response_code,
            replace_name=replace_name,
            tenant=tenant,
        )
        logger.warning("update wecom callback card ok user=%s response=%s", _mask(userid), response)
    except Exception:
        logger.exception("update wecom callback card failed user=%s", _mask(userid))


async def _update_card_with_button(
    *,
    db: AsyncSession,
    tenant: WecomTenantConfig,
    userid: str,
    response_code: str | None,
    title: str,
    description: str,
    log_id: str,
    button_text: str | None = None,
    action: str,
    buttons: list[dict[str, str | int]] | None = None,
) -> None:
    clean_response_code = _clean(response_code)
    if not clean_response_code:
        return
    try:
        card_title, _, horizontal_items = await _load_card_visit_context(db, log_id)
        response = await update_wecom_button_interaction_card(
            to_user=userid,
            response_code=clean_response_code,
            title=card_title if horizontal_items else title,
            description=description,
            main_title_desc=title if horizontal_items else None,
            horizontal_content_list=horizontal_items,
            task_id=build_recording_card_task_id(log_id, action=f"{action}_{int(time.time())}"),
            buttons=buttons or [
                {
                    "text": button_text,
                    "key": build_recording_action_key(action, log_id),
                    "style": 1,
                }
            ],
            tenant=tenant,
        )
        logger.warning("update wecom callback card button ok user=%s action=%s response=%s", _mask(userid), action, response)
    except Exception:
        logger.exception("update wecom callback card button failed user=%s action=%s", _mask(userid), action)


def _recording_card_button(text: str, action: str, log_id: str, *, style: int = 1) -> dict[str, str | int]:
    return {"text": text, "key": build_recording_action_key(action, log_id), "style": style}


def _start_stop_buttons(log_id: str) -> list[dict[str, str | int]]:
    return [
        _recording_card_button("开始录音", "start", log_id, style=1),
        _recording_card_button("停止录音", "stop", log_id, style=2),
    ]


async def _update_card_to_power_on_retry(
    *,
    db: AsyncSession,
    tenant: WecomTenantConfig,
    userid: str,
    response_code: str | None,
    log_id: str,
) -> None:
    await _update_card_with_button(
        db=db,
        tenant=tenant,
        userid=userid,
        response_code=response_code,
        title="工牌离线",
        description="系统检测到工牌未开机或暂未联网。请开机并等待联网后点击“我已开机”，再重新开始录音。",
        log_id=log_id,
        button_text="我已开机",
        action="retry",
    )


async def _update_card_to_start_retry(
    *,
    db: AsyncSession,
    tenant: WecomTenantConfig,
    userid: str,
    response_code: str | None,
    log_id: str,
) -> None:
    await _update_card_with_button(
        db=db,
        tenant=tenant,
        userid=userid,
        response_code=response_code,
        title="工牌已开机",
        description="请确认工牌已联网，然后点击下方按钮控制本次接诊录音。",
        log_id=log_id,
        action="start",
        buttons=_start_stop_buttons(log_id),
    )


async def _update_card_to_recording(
    *,
    db: AsyncSession,
    tenant: WecomTenantConfig,
    userid: str,
    response_code: str | None,
    log_id: str,
) -> None:
    await _update_card_with_button(
        db=db,
        tenant=tenant,
        userid=userid,
        response_code=response_code,
        title="工牌录音中",
        description="已开始本次接诊录音。接诊结束后可点击下方按钮停止录音，录音上传后会自动关联当前到诊单。",
        log_id=log_id,
        action="stop",
        buttons=[_recording_card_button("停止录音", "stop", log_id, style=2)],
    )


async def _update_card_to_stopped(
    *,
    db: AsyncSession,
    tenant: WecomTenantConfig,
    userid: str,
    response_code: str | None,
    log_id: str,
) -> None:
    await _update_card_with_button(
        db=db,
        tenant=tenant,
        userid=userid,
        response_code=response_code,
        title="录音已停止",
        description="录音已停止，录音上传后系统会继续处理；如果本次录音由此卡片启动，系统会自动关联当前到诊单。这张卡片已完成，不能再次开始新的录音。",
        log_id=log_id,
        action="done",
        buttons=[_recording_card_button("录音已完成", "done", log_id, style=2)],
    )


async def _update_card_to_not_recording(
    *,
    db: AsyncSession,
    tenant: WecomTenantConfig,
    userid: str,
    response_code: str | None,
    log_id: str,
) -> None:
    await _update_card_with_button(
        db=db,
        tenant=tenant,
        userid=userid,
        response_code=response_code,
        title="当前未录音",
        description="系统检测到当前工牌未处于录音状态，无需停止。需要开始接诊录音时可点击下方按钮。",
        log_id=log_id,
        action="start",
        buttons=[_recording_card_button("开始录音", "start", log_id, style=1)],
    )


async def _update_card_to_confirm_restart(
    *,
    db: AsyncSession,
    tenant: WecomTenantConfig,
    userid: str,
    response_code: str | None,
    log_id: str,
) -> None:
    await _update_card_with_button(
        db=db,
        tenant=tenant,
        userid=userid,
        response_code=response_code,
        title="工牌正在录音",
        description="检测到你的工牌正在录音。确认后系统会先停止当前录音，再开始新的接诊录音；取消则保持当前录音不变。",
        log_id=log_id,
        action="confirm",
        buttons=[
            _recording_card_button("停止并开始", "confirm", log_id, style=1),
            _recording_card_button("取消", "cancel", log_id, style=2),
        ],
    )


async def _send_text_safe(
    tenant: WecomTenantConfig,
    userid: str,
    content: str,
    *,
    response_code: str | None = None,
    card_button_text: str = "已处理",
) -> None:
    await _send_card_button_feedback(tenant, userid, response_code, card_button_text)
    try:
        response = await send_wecom_text_message(
            to_user=userid,
            content=content,
            tenant=tenant,
            enable_duplicate_check=False,
        )
        logger.warning("send wecom callback text ok user=%s response=%s", _mask(userid), response)
    except Exception:
        logger.exception("send wecom callback text failed user=%s", _mask(userid))


def _http_error_message(exc: HTTPException) -> str:
    detail = getattr(exc, "detail", None)
    return str(detail or "操作失败")


async def _load_recording_context_or_notify(
    db: AsyncSession,
    user: User,
    tenant: WecomTenantConfig,
    userid: str,
    *,
    response_code: str | None = None,
):
    try:
        return await _require_my_badge_recording_context(db, user)
    except HTTPException as exc:
        await _send_text_safe(
            tenant,
            userid,
            _http_error_message(exc),
            response_code=response_code,
            card_button_text="无法控制",
        )
        return None


async def _load_recording_context_silent(db: AsyncSession, user: User):
    try:
        return await _require_my_badge_recording_context(db, user)
    except HTTPException:
        return None


async def _control_recording(
    *,
    db: AsyncSession,
    user: User,
    context: Any,
    request: Request,
    action: str,
) -> tuple[bool, str]:
    if context.online is False:
        return False, "工牌未开机，请先开机后再开始录音。"
    try:
        if context.remote_provider == "iot":
            await iot_control_recording(action=action, device_no=context.device.device_code)
        else:
            await dvi_control_recording(
                action=action,
                team_code=context.team_code or "",
                user_id=context.user_id or "",
            )
    except (DingTalkConfigError, DingTalkApiError) as exc:
        detail = _handle_dingtalk_error(exc).detail
        return False, str(detail or exc)

    action_name = "开始录音" if action == "start" else "停止录音"
    await append_audit_log(
        db,
        operator_name=user.display_name or user.username,
        ip_address=request.client.host if request.client else "",
        module_name="企业微信接诊卡片",
        action_name=action_name,
        content=f"{action_name}：工牌 {context.device.device_code}",
    )
    return True, "录音已启动。" if action == "start" else "录音已停止。"


async def _handle_start_action(
    *,
    db: AsyncSession,
    request: Request,
    tenant: WecomTenantConfig,
    userid: str,
    user: User,
    log_id: str,
    response_code: str | None = None,
) -> None:
    if await _card_recording_already_started(db, log_id):
        await _handle_reused_start_action(
            db=db,
            tenant=tenant,
            userid=userid,
            user=user,
            log_id=log_id,
            response_code=response_code,
        )
        return

    context = await _load_recording_context_or_notify(db, user, tenant, userid, response_code=response_code)
    if context is None:
        return
    if context.online is False:
        await _update_card_to_power_on_retry(
            db=db,
            tenant=tenant,
            userid=userid,
            response_code=response_code,
            log_id=log_id,
        )
        await _send_text_safe(
            tenant,
            userid,
            "工牌未开机，请先开机后再开始录音。",
            response_code=None,
            card_button_text="工牌离线",
        )
        return
    if context.is_recording:
        await _update_card_to_confirm_restart(
            db=db,
            tenant=tenant,
            userid=userid,
            response_code=response_code,
            log_id=log_id,
        )
        return
    ok, message = await _control_recording(db=db, user=user, context=context, request=request, action="start")
    if ok:
        await mark_visit_order_card_recording_started(
            db,
            log_id=log_id,
            staff_id=getattr(getattr(context, "staff", None), "id", None),
            device_id=getattr(getattr(context, "device", None), "id", None),
            device_code=getattr(getattr(context, "device", None), "device_code", None),
        )
        await _update_card_to_recording(
            db=db,
            tenant=tenant,
            userid=userid,
            response_code=response_code,
            log_id=log_id,
        )
    await _send_text_safe(
        tenant,
        userid,
        message if ok else f"开始录音失败：{message}",
        response_code=None if ok else response_code,
        card_button_text="已开始录音" if ok else "启动失败",
    )


async def _handle_reused_start_action(
    *,
    db: AsyncSession,
    tenant: WecomTenantConfig,
    userid: str,
    user: User,
    log_id: str,
    response_code: str | None = None,
) -> None:
    context = await _load_recording_context_silent(db, user)
    if context is not None and context.online is not False and context.is_recording:
        await _update_card_to_recording(
            db=db,
            tenant=tenant,
            userid=userid,
            response_code=response_code,
            log_id=log_id,
        )
        message = "这张到诊卡片已经成功发起过一次录音，不能再次开始新的录音。当前工牌仍在录音，可点击停止录音结束本次录音。"
    else:
        await _update_card_to_stopped(
            db=db,
            tenant=tenant,
            userid=userid,
            response_code=response_code,
            log_id=log_id,
        )
        message = "这张到诊卡片已经成功发起过一次录音，不能再次开始新的录音。"
    await _send_text_safe(
        tenant,
        userid,
        message,
        response_code=None,
    )


async def _handle_stop_action(
    *,
    db: AsyncSession,
    request: Request,
    tenant: WecomTenantConfig,
    userid: str,
    user: User,
    log_id: str,
    response_code: str | None = None,
) -> None:
    context = await _load_recording_context_or_notify(db, user, tenant, userid, response_code=response_code)
    if context is None:
        return
    if context.online is False:
        await _update_card_to_power_on_retry(
            db=db,
            tenant=tenant,
            userid=userid,
            response_code=response_code,
            log_id=log_id,
        )
        await _send_text_safe(
            tenant,
            userid,
            "工牌未开机或未联网，无法停止录音。请先确认工牌已开机联网。",
            response_code=None,
            card_button_text="工牌离线",
        )
        return
    if not context.is_recording:
        if await _card_recording_already_started(db, log_id):
            await _update_card_to_stopped(
                db=db,
                tenant=tenant,
                userid=userid,
                response_code=response_code,
                log_id=log_id,
            )
        else:
            await _update_card_to_not_recording(
                db=db,
                tenant=tenant,
                userid=userid,
                response_code=response_code,
                log_id=log_id,
            )
        await _send_text_safe(
            tenant,
            userid,
            "当前工牌未处于录音状态，无需停止。",
            response_code=None,
        )
        return

    ok, message = await _control_recording(db=db, user=user, context=context, request=request, action="stop")
    if ok:
        await _update_card_to_stopped(
            db=db,
            tenant=tenant,
            userid=userid,
            response_code=response_code,
            log_id=log_id,
        )
    await _send_text_safe(
        tenant,
        userid,
        message if ok else f"停止录音失败：{message}",
        response_code=None if ok else response_code,
        card_button_text="已停止录音" if ok else "停止失败",
    )


async def _handle_confirm_action(
    *,
    db: AsyncSession,
    request: Request,
    tenant: WecomTenantConfig,
    userid: str,
    user: User,
    log_id: str,
    response_code: str | None = None,
) -> None:
    if await _card_recording_already_started(db, log_id):
        await _handle_reused_start_action(
            db=db,
            tenant=tenant,
            userid=userid,
            user=user,
            log_id=log_id,
            response_code=response_code,
        )
        return

    context = await _load_recording_context_or_notify(db, user, tenant, userid, response_code=response_code)
    if context is None:
        return
    if context.online is False:
        await _update_card_to_power_on_retry(
            db=db,
            tenant=tenant,
            userid=userid,
            response_code=response_code,
            log_id=log_id,
        )
        await _send_text_safe(
            tenant,
            userid,
            "工牌未开机，请先开机后再开始录音。",
            response_code=None,
            card_button_text="工牌离线",
        )
        return
    if context.is_recording:
        ok, message = await _control_recording(db=db, user=user, context=context, request=request, action="stop")
        if not ok:
            await _send_text_safe(
                tenant,
                userid,
                f"停止当前录音失败：{message}",
                response_code=response_code,
                card_button_text="停止失败",
            )
            return
        context = await _load_recording_context_or_notify(db, user, tenant, userid, response_code=response_code)
        if context is None:
            return
    ok, message = await _control_recording(db=db, user=user, context=context, request=request, action="start")
    if ok:
        await mark_visit_order_card_recording_started(
            db,
            log_id=log_id,
            staff_id=getattr(getattr(context, "staff", None), "id", None),
            device_id=getattr(getattr(context, "device", None), "id", None),
            device_code=getattr(getattr(context, "device", None), "device_code", None),
        )
        await _update_card_to_recording(
            db=db,
            tenant=tenant,
            userid=userid,
            response_code=response_code,
            log_id=log_id,
        )
    await _send_text_safe(
        tenant,
        userid,
        message if ok else f"开始录音失败：{message}",
        response_code=None if ok else response_code,
        card_button_text="已开始录音" if ok else "启动失败",
    )


async def _handle_retry_action(
    *,
    db: AsyncSession,
    tenant: WecomTenantConfig,
    userid: str,
    user: User,
    log_id: str,
    response_code: str | None = None,
) -> None:
    if await _card_recording_already_started(db, log_id):
        await _handle_reused_start_action(
            db=db,
            tenant=tenant,
            userid=userid,
            user=user,
            log_id=log_id,
            response_code=response_code,
        )
        return

    await _update_card_to_start_retry(
        db=db,
        tenant=tenant,
        userid=userid,
        response_code=response_code,
        log_id=log_id,
    )
    await _send_text_safe(
        tenant,
        userid,
        "已恢复“开始录音”按钮，请确认工牌在线后再次点击开始录音。",
        response_code=None,
    )


async def _process_button_event(
    *,
    db: AsyncSession,
    request: Request,
    callback: _CallbackContext,
    values: dict[str, str],
) -> None:
    userid = _field(values, "FromUserName", "FromUserId", "UserId")
    if not userid:
        logger.warning("ignore wecom callback without userid fields=%s", _field_summary(values))
        return
    action, log_id, source = _find_recording_action(values)
    if not action or not log_id:
        logger.warning(
            "ignore wecom callback without recording action user=%s fields=%s keys=%s",
            _mask(userid),
            _field_summary(values),
            sorted(values.keys())[:30],
        )
        return
    logger.warning(
        "wecom recording callback action=%s log_id=%s source=%s user=%s fields=%s",
        action,
        log_id,
        source,
        _mask(userid),
        _field_summary(values),
    )

    response_code = _field(values, "ResponseCode", "response_code")
    await _remember_response_code(db, log_id=log_id, response_code=response_code)

    _, user = await _load_user_by_wecom_userid(db, userid=userid, corp_id=callback.tenant.corp_id)
    if user is None:
        logger.warning("wecom recording callback user not bound user=%s corp=%s", _mask(userid), callback.tenant.corp_id)
        await _send_text_safe(callback.tenant, userid, "当前企业微信账号未绑定工牌系统账号，无法控制工牌录音。")
        return

    if action == "cancel":
        await _update_card_to_start_retry(
            db=db,
            tenant=callback.tenant,
            userid=userid,
            response_code=response_code,
            log_id=log_id,
        )
        await _send_text_safe(
            callback.tenant,
            userid,
            "已取消开始录音。",
            response_code=None,
        )
        return
    elif action == "start":
        await _handle_start_action(
            db=db,
            request=request,
            tenant=callback.tenant,
            userid=userid,
            user=user,
            log_id=log_id,
            response_code=response_code,
        )
    elif action == "stop":
        await _handle_stop_action(
            db=db,
            request=request,
            tenant=callback.tenant,
            userid=userid,
            user=user,
            log_id=log_id,
            response_code=response_code,
        )
    elif action == "confirm":
        await _handle_confirm_action(
            db=db,
            request=request,
            tenant=callback.tenant,
            userid=userid,
            user=user,
            log_id=log_id,
            response_code=response_code,
        )
    elif action == "retry":
        await _handle_retry_action(
            db=db,
            tenant=callback.tenant,
            userid=userid,
            user=user,
            log_id=log_id,
            response_code=response_code,
        )
    elif action == "done":
        await _send_text_safe(
            callback.tenant,
            userid,
            "这张到诊卡片已完成一次录音控制，不能再次开始新的录音。",
            response_code=response_code,
            card_button_text="录音已完成",
        )


@router.get("", response_class=PlainTextResponse)
async def verify_wecom_callback_url(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
    tenant_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    callback = await _resolve_callback_context(db, tenant_id)
    try:
        plain = decrypt_callback_payload(
            token=callback.token,
            aes_key=callback.aes_key,
            corp_id=callback.tenant.corp_id,
            msg_signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
            payload=echostr,
        )
    except WecomCallbackCryptoError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return PlainTextResponse(plain)


@router.post("", response_class=PlainTextResponse)
async def receive_wecom_callback(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    tenant_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    callback = await _resolve_callback_context(db, tenant_id)
    body = (await request.body()).decode("utf-8")
    try:
        plain_xml = decrypt_callback_payload(
            token=callback.token,
            aes_key=callback.aes_key,
            corp_id=callback.tenant.corp_id,
            msg_signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
            payload=body,
        )
        values = parse_xml_flat_texts(plain_xml)
        logger.warning(
            "wecom callback received tenant=%s user=%s fields=%s",
            callback.tenant.id,
            _mask(_field(values, "FromUserName", "FromUserId", "UserId")),
            _field_summary(values),
        )
    except WecomCallbackCryptoError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    try:
        await _process_button_event(db=db, request=request, callback=callback, values=values)
    except Exception:
        logger.exception("process wecom callback failed")
    return PlainTextResponse("success")
