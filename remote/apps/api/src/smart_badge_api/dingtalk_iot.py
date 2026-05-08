"""Client for DingTalk device-management IOT open APIs."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from threading import Lock
from time import time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from smart_badge_api.core.config import get_settings
from smart_badge_api.dingtalk import DingTalkApiError, DingTalkConfigError

logger = logging.getLogger("smart_badge.dingtalk_iot")
TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

_IOT_TOKEN_CACHE: dict[str, str | datetime | None] = {
    "value": None,
    "expires_at": None,
}
_IOT_TOKEN_LOCK = Lock()

# 共享 httpx AsyncClient + token singleflight。
_IOT_HTTP_CLIENT: httpx.AsyncClient | None = None
_IOT_HTTP_CLIENT_LOCK: asyncio.Lock | None = None
_IOT_TOKEN_REFRESH_LOCK: asyncio.Lock | None = None


async def _get_shared_iot_client() -> httpx.AsyncClient:
    global _IOT_HTTP_CLIENT, _IOT_HTTP_CLIENT_LOCK
    if _IOT_HTTP_CLIENT_LOCK is None:
        _IOT_HTTP_CLIENT_LOCK = asyncio.Lock()
    if _IOT_HTTP_CLIENT is not None and not _IOT_HTTP_CLIENT.is_closed:
        return _IOT_HTTP_CLIENT
    async with _IOT_HTTP_CLIENT_LOCK:
        if _IOT_HTTP_CLIENT is None or _IOT_HTTP_CLIENT.is_closed:
            timeout = get_settings().dingtalk_iot_timeout_seconds
            limits = httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=30.0)
            _IOT_HTTP_CLIENT = httpx.AsyncClient(timeout=timeout, limits=limits)
        return _IOT_HTTP_CLIENT


async def close_shared_iot_client() -> None:
    global _IOT_HTTP_CLIENT
    client = _IOT_HTTP_CLIENT
    _IOT_HTTP_CLIENT = None
    if client is not None and not client.is_closed:
        await client.aclose()


def _get_iot_token_refresh_lock() -> asyncio.Lock:
    global _IOT_TOKEN_REFRESH_LOCK
    if _IOT_TOKEN_REFRESH_LOCK is None:
        _IOT_TOKEN_REFRESH_LOCK = asyncio.Lock()
    return _IOT_TOKEN_REFRESH_LOCK


def configured_iot_hospital_codes() -> set[str]:
    values = str(get_settings().dingtalk_iot_hospital_codes or "").replace("，", ",").split(",")
    return {item.strip() for item in values if item.strip()}


def is_iot_hospital_code(hospital_code: str | None) -> bool:
    normalized = str(hospital_code or "").strip()
    return bool(normalized and normalized in configured_iot_hospital_codes())


def ensure_dingtalk_iot_enabled() -> None:
    settings = get_settings()
    if not settings.dingtalk_iot_enabled:
        raise DingTalkConfigError("钉钉 IOT 设备接口未启用")
    if settings.dingtalk_iot_app_id and settings.dingtalk_iot_app_secret:
        return
    raise DingTalkConfigError("钉钉 IOT 设备接口未配置，请设置 DINGTALK_IOT_APP_ID 和 DINGTALK_IOT_APP_SECRET")


def _clear_iot_access_token_cache() -> None:
    with _IOT_TOKEN_LOCK:
        _IOT_TOKEN_CACHE["value"] = None
        _IOT_TOKEN_CACHE["expires_at"] = None


def _iot_sign(app_id: str, timestamp: int, app_secret: str) -> str:
    raw = f"{app_id}{timestamp}{app_secret}".encode("utf-8")
    return hashlib.md5(raw, usedforsecurity=False).hexdigest()


async def get_iot_access_token() -> str:
    ensure_dingtalk_iot_enabled()
    now = datetime.now(timezone.utc)
    cached_value = _IOT_TOKEN_CACHE.get("value")
    cached_expires = _IOT_TOKEN_CACHE.get("expires_at")
    if isinstance(cached_value, str) and isinstance(cached_expires, datetime) and cached_expires > now:
        return cached_value

    async with _get_iot_token_refresh_lock():
        cached_value = _IOT_TOKEN_CACHE.get("value")
        cached_expires = _IOT_TOKEN_CACHE.get("expires_at")
        if isinstance(cached_value, str) and isinstance(cached_expires, datetime) and cached_expires > now:
            return cached_value

        settings = get_settings()
        timestamp = int(time())
        body = {
            "timestamp": timestamp,
            "appId": settings.dingtalk_iot_app_id,
            "sign": _iot_sign(settings.dingtalk_iot_app_id, timestamp, settings.dingtalk_iot_app_secret),
        }
        client = await _get_shared_iot_client()
        try:
            resp = await client.post(
                f"{settings.dingtalk_iot_base_url.rstrip('/')}/auth/access_token",
                json=body,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise DingTalkApiError(f"钉钉 IOT access_token 请求失败：{exc}") from exc

        payload = resp.json()
        if int(payload.get("result") or 0) != 1:
            raise DingTalkApiError(
                f"钉钉 IOT access_token 返回失败：{payload.get('msg') or payload}",
                code=str(payload.get("code") or ""),
            )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        access_token = str(data.get("accessToken") or "").strip()
        if not access_token:
            raise DingTalkApiError("钉钉 IOT access_token 返回为空")
        expires_at = datetime.fromtimestamp(int(data.get("expiredTime") or (timestamp + 86400)), tz=timezone.utc) - timedelta(
            seconds=120
        )
        with _IOT_TOKEN_LOCK:
            _IOT_TOKEN_CACHE["value"] = access_token
            _IOT_TOKEN_CACHE["expires_at"] = expires_at
        return access_token


async def _iot_request(
    path: str,
    *,
    method: str = "POST",
    json_body: dict[str, Any] | None = None,
    retry: bool = True,
) -> dict[str, Any]:
    settings = get_settings()
    access_token = await get_iot_access_token()
    client = await _get_shared_iot_client()
    try:
        resp = await client.request(
            method.upper(),
            f"{settings.dingtalk_iot_base_url.rstrip('/')}{path}",
            headers={
                "Authorization": access_token,
                "Content-Type": "application/json",
            },
            json=json_body or {} if method.upper() != "GET" else None,
        )
    except httpx.HTTPError as exc:
        raise DingTalkApiError(f"钉钉 IOT 接口请求失败 {path}：{exc}") from exc

    if resp.status_code in {401, 403} and retry:
        _clear_iot_access_token_cache()
        return await _iot_request(path, method=method, json_body=json_body, retry=False)
    if resp.status_code >= 400:
        raise DingTalkApiError(f"钉钉 IOT 接口 HTTP {resp.status_code}: {resp.text[:300]}")

    payload = resp.json()
    if int(payload.get("result") or 0) != 1:
        raise DingTalkApiError(
            f"钉钉 IOT 接口返回失败 {path}：{payload.get('msg') or payload}",
            code=str(payload.get("code") or ""),
        )
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _clean_text(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _coerce_timestamp_ms(value: object) -> int | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _coerce_iot_datetime_ms(value: object) -> int | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ_SHANGHAI)
    return int(parsed.timestamp() * 1000)


def _format_iot_datetime(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _default_audio_window() -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    lookback_days = max(get_settings().dingtalk_iot_audio_default_lookback_days, 1)
    return int((now - timedelta(days=lookback_days)).timestamp() * 1000), int(now.timestamp() * 1000)


def _iot_status_text(value: object) -> str | None:
    status = _coerce_int(value)
    if status == 1:
        return "online"
    if status == 0:
        return "offline"
    return None


def iot_device_to_dvi_device(item: dict[str, Any]) -> dict[str, Any] | None:
    sn = _clean_text(item.get("deviceNo"))
    if not sn:
        return None
    status = _iot_status_text(item.get("onlineStatus"))
    battery = _coerce_int(item.get("remainPower"))
    report_time = _clean_text(item.get("reportTime")) or _clean_text(item.get("lastOnlineTime"))
    timestamp = _coerce_iot_datetime_ms(report_time)
    normalized: dict[str, Any] = {
        "sn": sn,
        "name": sn,
        "status": {"value": status, "timestamp": timestamp} if status and timestamp else status,
        "battery": {"value": battery, "timestamp": timestamp} if battery is not None and timestamp else battery,
        "firmware": item.get("firmwareVersion"),
        "deviceType": item.get("deviceType"),
        "recordStatus": item.get("recordStatus"),
        "gpsStatus": item.get("gpsStatus"),
        "recordingStartTime": None,
        "lastRecordTime": item.get("lastRecordTime"),
        "lastOnlineTime": item.get("lastOnlineTime"),
        "lastOfflineTime": item.get("lastOfflineTime"),
        "reportTime": item.get("reportTime"),
        "remainStorageSize": item.get("remainStorageSize"),
        "pendingFileNum": item.get("pendingFileNum"),
        "chargeStatus": item.get("chargeStatus"),
        "deviceGroupName": item.get("deviceGroupName"),
        "employeeId": item.get("employeeId"),
        "employeeName": item.get("employeeName"),
        "departmentId": item.get("departmentId"),
        "departmentName": item.get("departmentName"),
        "storeId": item.get("storeId"),
        "storeName": item.get("storeName"),
        "remoteProvider": "iot",
        "iotAvailable": True,
        "dviAvailable": False,
        "source": "iot",
    }
    if _coerce_int(item.get("recordStatus")) == 1:
        normalized["status"] = {"value": "recording", "timestamp": timestamp} if timestamp else "recording"
        normalized["recordingStartTime"] = timestamp
    return normalized


def _iot_audio_file_id(item: dict[str, Any], *, device_no: str | None = None) -> str | None:
    raw_id = _clean_text(item.get("eventId")) or _clean_text(item.get("minioId"))
    if not raw_id:
        parts = [
            device_no or _clean_text(item.get("deviceNo")) or "unknown",
            _clean_text(item.get("startTime")) or "",
            _clean_text(item.get("stopTime")) or "",
            _clean_text(item.get("seq")) or "",
        ]
        raw_id = "_".join(part for part in parts if part)
    return f"iot:{raw_id}" if raw_id else None


def iot_audio_to_dvi_audio(item: dict[str, Any], *, fallback_device_no: str | None = None) -> dict[str, Any] | None:
    device_no = _clean_text(item.get("deviceNo")) or _clean_text(fallback_device_no)
    file_id = _iot_audio_file_id(item, device_no=device_no)
    if not device_no or not file_id:
        return None

    start_ms = _coerce_iot_datetime_ms(item.get("startTime"))
    stop_ms = _coerce_iot_datetime_ms(item.get("stopTime"))
    duration_ms = None
    seconds = _coerce_int(item.get("seconds"))
    if seconds is not None and seconds >= 0:
        duration_ms = seconds * 1000
    elif start_ms is not None and stop_ms is not None and stop_ms > start_ms:
        duration_ms = stop_ms - start_ms

    download_url = _clean_text(item.get("fileUrl"))
    normalized: dict[str, Any] = {
        "sn": device_no,
        "fileId": file_id,
        "iotEventId": _clean_text(item.get("eventId")),
        "minioId": _clean_text(item.get("minioId")),
        "orderNo": _clean_text(item.get("orderNo")),
        "fileName": _clean_text(item.get("fileName")) or f"{device_no}_{start_ms or 'audio'}.mp3",
        "fileSize": _coerce_int(item.get("fileSize")),
        "duration": duration_ms,
        "seconds": seconds,
        "createTime": start_ms or stop_ms,
        "startTime": item.get("startTime"),
        "stopTime": item.get("stopTime"),
        "downloadUrl": download_url,
        "fileUrl": download_url,
        "rightFileUrl": _clean_text(item.get("rightFileUrl")),
        "leftFileUrl": _clean_text(item.get("leftFileUrl")),
        "audioFileExpirationTime": item.get("audioFileExpirationTime"),
        "seq": _coerce_int(item.get("seq")),
        "endMark": _coerce_int(item.get("endMark")),
        "recType": _coerce_int(item.get("recType")),
        "remoteProvider": "iot",
        "source": "iot",
    }
    return normalized


async def iot_query_devices(
    *,
    page: int = 1,
    size: int = 100,
    device_no: str | None = None,
    device_type: str | None = None,
    device_group_name: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "page": page,
        "size": min(max(size, 1), 100),
    }
    if device_no:
        body["deviceNo"] = device_no
    if device_type:
        body["deviceType"] = device_type
    if device_group_name:
        body["deviceGroupName"] = device_group_name
    return await _iot_request("/device/query_all", json_body=body)


async def iot_list_devices(*, device_no: str | None = None, max_pages: int = 20) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = await iot_query_devices(page=page, size=100, device_no=device_no)
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        for row in rows:
            if isinstance(row, dict) and (normalized := iot_device_to_dvi_device(row)) is not None:
                devices.append(normalized)
        pages = _coerce_int(payload.get("pages")) or page
        if device_no or page >= pages:
            break
    return devices


async def iot_query_audio_files(
    *,
    page: int = 1,
    size: int = 100,
    device_no: str | None = None,
    order_no: str | None = None,
    start_timestamp: int | None = None,
    end_timestamp: int | None = None,
    upload_status: int | None = 1,
) -> dict[str, Any]:
    if start_timestamp is None or end_timestamp is None:
        default_start, default_end = _default_audio_window()
        start_timestamp = default_start if start_timestamp is None else start_timestamp
        end_timestamp = default_end if end_timestamp is None else end_timestamp

    body: dict[str, Any] = {
        "page": page,
        "size": min(max(size, 1), 100),
        "startTime": _format_iot_datetime(start_timestamp),
        "stopTime": _format_iot_datetime(end_timestamp),
    }
    if device_no:
        body["deviceNo"] = device_no
    if order_no:
        body["orderNo"] = order_no
    if upload_status is not None:
        body["uploadStatus"] = upload_status
    return await _iot_request("/audio/query_all", json_body=body)


async def iot_list_audio_files(
    *,
    device_no: str | None = None,
    start_timestamp: int | None = None,
    end_timestamp: int | None = None,
    max_pages: int = 50,
    upload_status: int | None = 1,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = await iot_query_audio_files(
            page=page,
            size=100,
            device_no=device_no,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            upload_status=upload_status,
        )
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        for row in rows:
            if isinstance(row, dict) and (normalized := iot_audio_to_dvi_audio(row, fallback_device_no=device_no)):
                items.append(normalized)
        pages = _coerce_int(payload.get("pages")) or page
        if page >= pages:
            break
    return items


async def iot_query_device_statuses(sn_list: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sn in list(dict.fromkeys(str(item).strip() for item in sn_list if str(item or "").strip())):
        rows.extend(await iot_list_devices(device_no=sn, max_pages=1))
    return rows


async def iot_control_recording(*, action: str, device_no: str, order_no: str | None = None) -> dict[str, Any]:
    normalized_action = action.strip().lower()
    if normalized_action not in {"start", "stop"}:
        raise ValueError("action must be start or stop")
    body = {"deviceNo": device_no}
    if order_no:
        body["orderNo"] = order_no
    await _iot_request(f"/audio/{normalized_action}", json_body=body)
    return {"success": True, "source": "iot"}


async def iot_control_gps(*, action: str, device_no: str, order_no: str | None = None) -> dict[str, Any]:
    normalized_action = action.strip().lower()
    if normalized_action not in {"start", "stop", "curr_location"}:
        raise ValueError("action must be start, stop or curr_location")
    body = {"deviceNo": device_no}
    if order_no:
        body["orderNo"] = order_no
    path = "/device/gps/curr_location" if normalized_action == "curr_location" else f"/device/gps/{normalized_action}"
    await _iot_request(path, json_body=body)
    return {"success": True, "source": "iot"}


async def iot_update_device_settings(
    *,
    device_nos: list[str],
    allow_clip: int,
    clip_seconds: int,
    fail_seconds: int | None = None,
    allow_play: int | None = None,
    audio_volume: int | None = None,
    gps_enable: int | None = None,
    auto_upload: int | None = None,
    environment_id: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "deviceNos": [item for item in dict.fromkeys(sn.strip() for sn in device_nos if sn.strip())],
        "allowClip": allow_clip,
        "clipSeconds": clip_seconds,
    }
    optional = {
        "failSeconds": fail_seconds,
        "allowPlay": allow_play,
        "audioVolume": audio_volume,
        "gpsEnable": gps_enable,
        "autoUpload": auto_upload,
        "environmentId": environment_id,
    }
    body.update({key: value for key, value in optional.items() if value is not None})
    return await _iot_request("/device/setting", json_body=body)


async def iot_batch_assign_employees(*, binds: list[dict[str, Any]], operator_id: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"binds": binds[:500]}
    if operator_id is not None:
        body["operatorId"] = operator_id
    return await _iot_request("/device/batch-assign-employees", json_body=body)


async def iot_submit_audio_task(
    *,
    device_no: str,
    start_time: str,
    end_time: str,
    callback_url: str | None = None,
    ori_task_id: str | None = None,
    order_no: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "deviceNo": device_no,
        "timeSlots": [{"startTime": start_time, "endTime": end_time}],
    }
    if callback_url:
        body["callbackUrl"] = callback_url
    if ori_task_id:
        body["oriTaskId"] = ori_task_id
    if order_no:
        body["orderNo"] = order_no
    return await _iot_request("/audio/task/submit", json_body=body)


async def iot_query_audio_task(task_id: str) -> dict[str, Any]:
    return await _iot_request(f"/audio/task/query/{task_id}", method="GET")


async def iot_voice_print_create(*, device_no: str, callback_url: str) -> dict[str, Any]:
    return await _iot_request("/voice-print/create", json_body={"deviceNo": device_no, "callbackUrl": callback_url})


async def iot_voice_print_update(*, device_no: str, callback_url: str, voice_print_id: str) -> dict[str, Any]:
    return await _iot_request(
        "/voice-print/update",
        json_body={"deviceNo": device_no, "callbackUrl": callback_url, "voicePrintId": voice_print_id},
    )


async def iot_voice_print_list(
    *,
    page: int = 1,
    size: int = 100,
    device_no: str | None = None,
    voice_print_id: str | None = None,
    start_time: str | None = None,
    stop_time: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"page": page, "size": min(max(size, 1), 100)}
    optional = {
        "deviceNo": device_no,
        "voicePrintId": voice_print_id,
        "startTime": start_time,
        "stopTime": stop_time,
    }
    body.update({key: value for key, value in optional.items() if value})
    return await _iot_request("/voice-print/list", json_body=body)


async def iot_voice_print_delete(voice_print_id: str) -> dict[str, Any]:
    return await _iot_request("/voice-print/delete", json_body={"voicePrintId": voice_print_id})
