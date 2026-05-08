from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from smart_badge_api.core.config import get_settings


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
            timeout = get_settings().message_push_timeout_seconds
            limits = httpx.Limits(max_keepalive_connections=10, max_connections=30, keepalive_expiry=30.0)
            _HTTP_CLIENT = httpx.AsyncClient(timeout=timeout, limits=limits)
        return _HTTP_CLIENT


async def close_shared_message_push_client() -> None:
    global _HTTP_CLIENT
    client = _HTTP_CLIENT
    _HTTP_CLIENT = None
    if client is not None and not client.is_closed:
        await client.aclose()


class MessagePushConfigError(RuntimeError):
    pass


class MessagePushApiError(RuntimeError):
    pass


def _clean_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _load_auth_code_map(raw: str) -> dict[str, str]:
    text = _clean_text(raw)
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        return {
            str(key).strip(): str(value).strip()
            for key, value in payload.items()
            if str(key).strip() and str(value).strip()
        }

    result: dict[str, str] = {}
    for item in text.replace(";", ",").split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            result[key] = value
    return result


def resolve_message_push_auth_code(hospital_code: str | None) -> str | None:
    code = _clean_text(hospital_code)
    if not code:
        return None
    auth_codes = _load_auth_code_map(get_settings().message_push_auth_codes)
    return auth_codes.get(code)


async def send_message_push(
    *,
    title: str,
    content: str,
    auth_code: str,
    targets: list[str],
    biz_user_id: str,
    org_code: str | None = None,
    msg_type: str = "text",
) -> dict[str, Any]:
    settings = get_settings()
    base_url = _clean_text(settings.message_push_base_url)
    if not base_url:
        raise MessagePushConfigError("消息推送平台地址未配置")
    normalized_targets = [target for target in (_clean_text(item) for item in targets) if target]
    if not normalized_targets:
        raise MessagePushConfigError("消息推送目标员工编号为空")

    body: dict[str, Any] = {
        "title": title,
        "content": content,
        "msg_type": msg_type,
        "biz_user_id": biz_user_id,
        "auth_code": auth_code,
        "targets": normalized_targets,
    }
    if org_code:
        body["org_code"] = org_code

    client = await _get_shared_client()
    response = await client.post(f"{base_url.rstrip('/')}/api/v1/messages", json=body)

    if response.status_code != 202:
        try:
            payload = response.json()
            detail = payload.get("detail") or payload
        except Exception:
            detail = response.text
        raise MessagePushApiError(f"消息推送平台发送失败 [{response.status_code}]: {detail}")

    return response.json()
