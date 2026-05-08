from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import Recording, RecordingVisitAnalysis, SapPushLog
from smart_badge_api.db.session import _session_factory
from smart_badge_api.sap_consultation import generate_sap_consultation_payloads
from smart_badge_api.sap_push_notifications import notify_sap_push_result

logger = logging.getLogger("smart_badge.sap_push")

SAP_FUNCTION_NAME = "ZMC_FM_INT_YMC_SET"
SAP_IM_TYPE = "YMC_2013"
SAP_RESULT_FIELD = "RE_DATA"


# 共享 httpx AsyncClient，避免每次推送新建连接池。
_HTTP_CLIENT: httpx.AsyncClient | None = None
_HTTP_CLIENT_LOCK: asyncio.Lock | None = None


async def _get_shared_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT, _HTTP_CLIENT_LOCK
    if _HTTP_CLIENT_LOCK is None:
        _HTTP_CLIENT_LOCK = asyncio.Lock()
    if _HTTP_CLIENT is not None and not _HTTP_CLIENT.is_closed:
        return _HTTP_CLIENT
    async with _HTTP_CLIENT_LOCK:
        if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
            timeout = get_settings().sap_rfc_timeout_seconds
            limits = httpx.Limits(max_keepalive_connections=10, max_connections=30, keepalive_expiry=30.0)
            _HTTP_CLIENT = httpx.AsyncClient(timeout=timeout, limits=limits)
        return _HTTP_CLIENT


async def close_shared_sap_push_client() -> None:
    global _HTTP_CLIENT
    client = _HTTP_CLIENT
    _HTTP_CLIENT = None
    if client is not None and not client.is_closed:
        await client.aclose()


class SapPushPreparationError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _mask_signature(signature: str) -> str:
    if not signature:
        return ""
    if len(signature) <= 12:
        return "***"
    return f"{signature[:8]}...{signature[-6:]}"


def _build_signature(app_id: str, timestamp: int, data: str, secret: str) -> str:
    if not secret.strip():
        return ""
    raw = f"{app_id}{timestamp}{data}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def build_sap_gateway_request(payload: dict[str, Any], *, timestamp: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    resolved_timestamp = int(timestamp if timestamp is not None else time.time())
    data = _json_dumps(
        {
            "functionName": SAP_FUNCTION_NAME,
            "imType": SAP_IM_TYPE,
            "imData": _json_dumps(payload),
            "resultField": SAP_RESULT_FIELD,
        }
    )
    return {
        "appId": settings.sap_rfc_app_id,
        "timestamp": resolved_timestamp,
        "data": data,
        "signature": _build_signature(
            settings.sap_rfc_app_id,
            resolved_timestamp,
            data,
            settings.sap_rfc_secret,
        ),
    }


def _mask_gateway_request_for_log(request_body: dict[str, Any]) -> dict[str, Any]:
    masked = dict(request_body)
    masked["signature"] = _mask_signature(str(masked.get("signature") or ""))
    return masked


def _parse_embedded_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text or not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_business_status(response_body: Any) -> tuple[int | str | None, str | None, str | None]:
    if not isinstance(response_body, dict):
        return None, None, None

    gateway_code = response_body.get("code")
    payload = _parse_embedded_dict(response_body.get("data")) or _parse_embedded_dict(response_body.get("msg"))
    if not isinstance(payload, dict):
        return gateway_code, None, str(response_body.get("msg") or "").strip() or None

    status = payload.get("STATU") or payload.get("statu") or payload.get("status")
    message = (
        payload.get("REMSG")
        or payload.get("remsg")
        or payload.get("message")
        or response_body.get("msg")
    )
    normalized_message = str(message).strip() if message else None
    return gateway_code, str(status).strip().upper() if status else None, normalized_message


def _build_response_item(request_index: int, http_status_code: int | None, response_body: Any) -> dict[str, Any]:
    gateway_code, business_status, business_message = _extract_business_status(response_body)
    success = bool(
        http_status_code == 200
        and (gateway_code in (None, 200, "200"))
        and (business_status in (None, "S"))
    )
    return {
        "request_index": request_index,
        "success": success,
        "http_status_code": http_status_code,
        "gateway_code": gateway_code,
        "business_status": business_status,
        "business_message": business_message,
        "response_body": response_body,
    }


async def _commit_final_push_log(db: AsyncSession, push_log: SapPushLog) -> None:
    await db.commit()
    await notify_sap_push_result(db, push_log)
    await db.refresh(push_log)


def _extract_existing_consultation_no(message: str | None) -> str | None:
    text = str(message or "").strip()
    if not text or "已有咨询单" not in text:
        return None
    matches = re.findall(r"咨询单[【\\[]?([A-Za-z0-9]+)[】\\]]?", text)
    if not matches:
        return None
    return str(matches[-1]).strip() or None


def _build_update_retry_payload(payload: dict[str, Any], consultation_no: str) -> dict[str, Any]:
    cloned = json.loads(json.dumps(payload, ensure_ascii=False))
    zxxx = cloned.get("zxxx")
    if not isinstance(zxxx, dict):
        zxxx = {}
        cloned["zxxx"] = zxxx
    zxxx["mode"] = "U"
    zxxx["zxdh"] = consultation_no
    return cloned


def _coerce_response_item(item: dict[str, Any], default_request_index: int) -> dict[str, Any]:
    http_status_code = item.get("http_status_code")
    response_body = item.get("response_body")
    if response_body is None:
        response_body = {
            "code": item.get("gateway_code"),
            "msg": item.get("business_message"),
            "data": None,
        }
    built = _build_response_item(
        int(item.get("request_index") or default_request_index),
        http_status_code,
        response_body,
    )
    built["attempt"] = int(item.get("attempt") or 1)
    built["payload_mode"] = str(item.get("payload_mode") or "").strip() or None
    built["retry_reason"] = str(item.get("retry_reason") or "").strip() or None
    built["superseded_by_retry"] = bool(item.get("superseded_by_retry"))
    return built


def _collapse_response_attempts(response_items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    normalized_items = [_coerce_response_item(item, index + 1) for index, item in enumerate(response_items) if isinstance(item, dict)]
    if not normalized_items:
        return [], None

    grouped: dict[int, list[dict[str, Any]]] = {}
    for item in normalized_items:
        grouped.setdefault(int(item["request_index"]), []).append(item)

    logical_results: list[dict[str, Any]] = []
    for request_index in sorted(grouped):
        attempts = grouped[request_index]
        attempts.sort(key=lambda item: int(item.get("attempt") or 1))
        logical_results.append(attempts[-1])

    failed_item = next((item for item in logical_results if not item["success"]), None)
    final_item = failed_item or logical_results[-1]
    return logical_results, final_item


def serialize_sap_push_log(log: SapPushLog) -> dict[str, Any]:
    result_summary = summarize_sap_push_log_result(log)
    return {
        "id": log.id,
        "recording_id": log.recording_id,
        "recording_file_name": log.recording.file_name if getattr(log, "recording", None) else None,
        "recording_created_at": log.recording.created_at.isoformat()
        if getattr(log, "recording", None) and getattr(log.recording, "created_at", None)
        else None,
        "visit_id": log.visit_id,
        "visit_order_no": log.visit_order_no,
        "visit_order_seg": log.visit_order_seg,
        "customer_name": log.customer_name,
        "customer_code": log.customer_code,
        "advisor_name": log.advisor_name,
        "trigger_mode": log.trigger_mode,
        "status": log.status,
        "send_enabled": bool(log.send_enabled),
        "initiated_by": log.initiated_by,
        "request_url": log.request_url,
        "trace_id": log.trace_id,
        "request_payloads": list(log.request_payloads or []),
        "gateway_requests": list(log.gateway_requests or []),
        "response_items": list(log.response_items or []),
        "http_status_code": log.http_status_code,
        "business_status": log.business_status,
        "business_message": log.business_message,
        "error_message": log.error_message,
        "effective_status": result_summary["effective_status"],
        "effective_business_status": result_summary["effective_business_status"],
        "effective_reason": result_summary["effective_reason"],
        "sent_at": log.sent_at.isoformat() if log.sent_at else None,
        "message_success_notified_at": log.message_success_notified_at.isoformat() if log.message_success_notified_at else None,
        "message_failure_notified_at": log.message_failure_notified_at.isoformat() if log.message_failure_notified_at else None,
        "message_notify_error": log.message_notify_error,
        "created_at": log.created_at.isoformat() if log.created_at else "",
        "updated_at": log.updated_at.isoformat() if log.updated_at else "",
    }


def summarize_sap_push_log_result(log: SapPushLog | dict[str, Any]) -> dict[str, Any]:
    response_items = list(getattr(log, "response_items", None) or (log.get("response_items") if isinstance(log, dict) else []) or [])
    stored_status = str(getattr(log, "status", None) or (log.get("status") if isinstance(log, dict) else "") or "").strip() or "prepared"
    error_message = str(getattr(log, "error_message", None) or (log.get("error_message") if isinstance(log, dict) else "") or "").strip() or None

    if error_message:
        return {
            "effective_status": "failed",
            "effective_business_status": "E",
            "effective_reason": error_message,
        }

    logical_results, final_item = _collapse_response_attempts(response_items)

    if logical_results:
        return {
            "effective_status": "succeeded" if all(item["success"] for item in logical_results) else "failed",
            "effective_business_status": final_item.get("business_status"),
            "effective_reason": final_item.get("business_message") or final_item.get("error_message"),
        }

    if stored_status in {"succeeded", "failed", "skipped", "queued", "sending", "prepared"}:
        return {
            "effective_status": stored_status,
            "effective_business_status": str(getattr(log, "business_status", None) or (log.get("business_status") if isinstance(log, dict) else "") or "").strip() or None,
            "effective_reason": str(getattr(log, "business_message", None) or (log.get("business_message") if isinstance(log, dict) else "") or "").strip() or None,
        }

    return {
        "effective_status": "prepared",
        "effective_business_status": None,
        "effective_reason": None,
    }


async def create_sap_push_log(
    db: AsyncSession,
    recording_id: str,
    *,
    target_visit_id: str | None = None,
    trigger_mode: str = "manual",
    initiated_by: str | None = None,
    prefer_async: bool = True,
) -> SapPushLog:
    preview = await generate_sap_consultation_payloads(db, recording_id, target_visit_id=target_visit_id)
    if "error" in preview:
        raise SapPushPreparationError(preview["error"], preview["message"])

    settings = get_settings()
    recording = await db.get(Recording, recording_id)
    dispatch_mode = settings.sap_rfc_dispatch_mode
    if not settings.sap_rfc_send_enabled:
        status = "skipped"
        error_message = "SAP RFC 回传已关闭，未发送外部请求"
    elif prefer_async and dispatch_mode in {"dramatiq", "background"}:
        status = "queued"
        error_message = None
    else:
        status = "prepared"
        error_message = None

    push_log = SapPushLog(
        recording_id=recording_id,
        visit_id=target_visit_id or (recording.visit_id if recording else None),
        visit_order_no=preview["visit_order_no"],
        visit_order_seg=preview.get("visit_order_seg"),
        customer_name=preview.get("customer_name"),
        customer_code=preview.get("customer_code"),
        advisor_name=preview.get("advisor_name"),
        trigger_mode=trigger_mode,
        status=status,
        send_enabled=bool(settings.sap_rfc_send_enabled),
        initiated_by=(initiated_by or "").strip() or None,
        request_url=settings.sap_rfc_gateway_url,
        request_payloads=preview["payloads"],
        gateway_requests=[],
        response_items=[],
        error_message=error_message,
    )
    db.add(push_log)
    await db.flush()
    if target_visit_id:
        analysis = (
            await db.execute(
                select(RecordingVisitAnalysis).where(
                    RecordingVisitAnalysis.recording_id == recording_id,
                    RecordingVisitAnalysis.visit_id == target_visit_id,
                )
            )
        ).scalar_one_or_none()
        if analysis is not None:
            analysis.sap_push_log_id = push_log.id
    await db.commit()
    await db.refresh(push_log)
    return push_log


async def execute_sap_push_log(push_log_id: str) -> SapPushLog | None:
    settings = get_settings()

    async with _session_factory() as db:
        push_log = await db.get(SapPushLog, push_log_id)
        if push_log is None:
            return None

        if not settings.sap_rfc_send_enabled:
            push_log.status = "skipped"
            push_log.error_message = "SAP RFC 回传已关闭，未发送外部请求"
            push_log.updated_at = _utcnow()
            await db.commit()
            await db.refresh(push_log)
            return push_log

        if not settings.sap_rfc_secret.strip():
            push_log.status = "failed"
            push_log.error_message = "缺少 SAP_RFC_SECRET，无法执行回传"
            push_log.updated_at = _utcnow()
            await _commit_final_push_log(db, push_log)
            return push_log

        request_payloads = list(push_log.request_payloads or [])
        if not request_payloads:
            push_log.status = "failed"
            push_log.error_message = "缺少咨询单 payload，无法执行回传"
            push_log.updated_at = _utcnow()
            await _commit_final_push_log(db, push_log)
            return push_log

        trace_id = secrets.token_hex(16)
        gateway_bodies = [build_sap_gateway_request(payload) for payload in request_payloads]
        push_log.trace_id = trace_id
        push_log.request_url = settings.sap_rfc_gateway_url
        push_log.gateway_requests = [_mask_gateway_request_for_log(body) for body in gateway_bodies]
        push_log.response_items = []
        push_log.http_status_code = None
        push_log.business_status = None
        push_log.business_message = None
        push_log.error_message = None
        push_log.status = "sending"
        push_log.updated_at = _utcnow()
        await db.commit()

        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "traceId": trace_id,
        }
        response_items: list[dict[str, Any]] = []

        try:
            client = await _get_shared_client()
            for index, body in enumerate(gateway_bodies, start=1):
                payload_mode = str((request_payloads[index - 1].get("zxxx") or {}).get("mode") or "").strip() or "C"
                response = await client.post(settings.sap_rfc_gateway_url, json=body, headers=headers)
                try:
                    response_body: Any = response.json()
                except ValueError:
                    response_body = {"raw_text": response.text}
                created_item = _build_response_item(index, response.status_code, response_body)
                created_item["attempt"] = 1
                created_item["payload_mode"] = payload_mode
                created_item["retry_reason"] = None
                created_item["superseded_by_retry"] = False
                response_items.append(created_item)

                existing_consultation_no = (
                    _extract_existing_consultation_no(created_item.get("business_message"))
                    if not created_item["success"] and payload_mode == "C"
                    else None
                )
                if not existing_consultation_no:
                    continue

                retry_payload = _build_update_retry_payload(request_payloads[index - 1], existing_consultation_no)
                retry_body = build_sap_gateway_request(retry_payload)
                push_log.gateway_requests = list(push_log.gateway_requests or []) + [_mask_gateway_request_for_log(retry_body)]
                await db.commit()

                retry_response = await client.post(settings.sap_rfc_gateway_url, json=retry_body, headers=headers)
                try:
                    retry_response_body: Any = retry_response.json()
                except ValueError:
                    retry_response_body = {"raw_text": retry_response.text}

                response_items[-1]["superseded_by_retry"] = True
                response_items[-1]["retry_reason"] = f"已有咨询单，改用咨询单号 {existing_consultation_no} 进行修改回传"
                retry_item = _build_response_item(index, retry_response.status_code, retry_response_body)
                retry_item["attempt"] = 2
                retry_item["payload_mode"] = "U"
                retry_item["retry_reason"] = f"使用已有咨询单号 {existing_consultation_no} 改为修改模式回传"
                retry_item["superseded_by_retry"] = False
                response_items.append(retry_item)
        except Exception as exc:
            logger.exception("sap push failed log_id=%s", push_log_id)
            push_log.status = "failed"
            push_log.error_message = f"{type(exc).__name__}: {exc}"
            push_log.sent_at = _utcnow()
            push_log.updated_at = _utcnow()
            push_log.response_items = response_items
            await _commit_final_push_log(db, push_log)
            return push_log

        push_log.response_items = response_items
        logical_results, final_item = _collapse_response_attempts(response_items)
        push_log.http_status_code = final_item["http_status_code"] if final_item else None
        if final_item:
            push_log.business_status = final_item.get("business_status")
        push_log.business_message = final_item.get("business_message")
        push_log.status = "succeeded" if logical_results and all(item["success"] for item in logical_results) else "failed"
        push_log.sent_at = _utcnow()
        push_log.updated_at = _utcnow()
        await _commit_final_push_log(db, push_log)
        return push_log


async def list_recording_sap_push_logs(db: AsyncSession, recording_id: str) -> list[SapPushLog]:
    return (
        await db.execute(
            select(SapPushLog)
            .where(SapPushLog.recording_id == recording_id)
            .order_by(SapPushLog.created_at.desc())
        )
    ).scalars().all()
