"""DingTalk open-platform client for smart badge (钉工牌) integration.

Handles access-token lifecycle and exposes typed helpers for the badge /
device PaaS APIs (DVI endpoints).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from smart_badge_api.core.config import get_settings

logger = logging.getLogger("smart_badge.dingtalk")

# ── token cache ────────────────────────────────────────────────────────

_ACCESS_TOKEN_CACHE: dict[str, str | datetime | None] = {
    "value": None,
    "expires_at": None,
}
# 单一刷新（singleflight）锁，避免 token 过期瞬间多协程并发刷新。
_ACCESS_TOKEN_REFRESH_LOCK: asyncio.Lock | None = None


def _access_token_refresh_lock() -> asyncio.Lock:
    global _ACCESS_TOKEN_REFRESH_LOCK
    if _ACCESS_TOKEN_REFRESH_LOCK is None:
        _ACCESS_TOKEN_REFRESH_LOCK = asyncio.Lock()
    return _ACCESS_TOKEN_REFRESH_LOCK

# 钉钉通讯录仍使用 OAPI；按员工号查询需要扫描部门人员列表，因此做短缓存。
_OAPI_BASE_URL = "https://oapi.dingtalk.com"
_CONTACT_DEPT_CONCURRENCY = 16
_CONTACT_USER_CONCURRENCY = 8
_CONTACT_DIRECTORY_TTL_SECONDS = 30 * 60
_CONTACT_DIRECTORY_CACHE: dict[str, Any] = {"expires_at": None, "by_job_number": {}}
_CONTACT_DIRECTORY_LOCK = asyncio.Lock()


class DingTalkConfigError(RuntimeError):
    pass


class DingTalkApiError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def ensure_dingtalk_enabled() -> None:
    if get_settings().dingtalk_enabled:
        return
    raise DingTalkConfigError(
        "钉钉工牌集成未配置，请设置 DINGTALK_CORP_ID、DINGTALK_APP_KEY  和 DINGTALK_APP_SECRET"
    )


# ── access-token ───────────────────────────────────────────────────────

def _clear_access_token_cache() -> None:
    _ACCESS_TOKEN_CACHE["value"] = None
    _ACCESS_TOKEN_CACHE["expires_at"] = None


def _cached_access_token(now: datetime) -> str | None:
    cached_value = _ACCESS_TOKEN_CACHE.get("value")
    cached_expires = _ACCESS_TOKEN_CACHE.get("expires_at")
    if (
        isinstance(cached_value, str)
        and isinstance(cached_expires, datetime)
        and cached_expires > now
    ):
        return cached_value
    return None


async def get_access_token() -> str:
    """Obtain a cached DingTalk access_token, refreshing when needed."""
    ensure_dingtalk_enabled()
    now = datetime.now(timezone.utc)
    cached = _cached_access_token(now)
    if cached is not None:
        return cached

    async with _access_token_refresh_lock():
        # 重新检查：可能已被其他协程刷新
        now = datetime.now(timezone.utc)
        cached = _cached_access_token(now)
        if cached is not None:
            return cached

        settings = get_settings()
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(
                    f"{settings.dingtalk_api_base_url.rstrip('/')}/v1.0/oauth2/accessToken",
                    json={
                        "appKey": settings.dingtalk_app_key,
                        "appSecret": settings.dingtalk_app_secret,
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise DingTalkApiError(f"钉钉 accessToken 请求失败：{exc}") from exc
        payload = resp.json()

        access_token = str(payload.get("accessToken") or "").strip()
        expire_in = int(payload.get("expireIn") or 7200)
        if not access_token:
            raise DingTalkApiError("钉钉 accessToken 返回为空")

        _ACCESS_TOKEN_CACHE["value"] = access_token
        _ACCESS_TOKEN_CACHE["expires_at"] = now + timedelta(seconds=expire_in - 120)

        return access_token


# ── generic request helper ─────────────────────────────────────────────

async def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    retry: bool = True,
) -> dict:
    """Call a DingTalk v1.0 / v2.0 API with automatic token injection."""
    settings = get_settings()
    access_token = await get_access_token()

    headers = {
        "x-acs-dingtalk-access-token": access_token,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.request(
                method.upper(),
                f"{settings.dingtalk_api_base_url.rstrip('/')}{path}",
                headers=headers,
                json=json_body,
                params=params,
            )
        except httpx.HTTPError as exc:
            raise DingTalkApiError(f"钉钉接口请求失败 {path}：{exc}") from exc

    # DingTalk returns 401 when token is invalid/expired
    if resp.status_code == 401 and retry:
        _clear_access_token_cache()
        return await _request(method, path, json_body=json_body, params=params, retry=False)

    if resp.status_code >= 400:
        body = resp.text
        try:
            err = resp.json()
            code = err.get("code", "")
            message = err.get("message", body)
        except Exception:
            code, message = "", body
        raise DingTalkApiError(
            f"钉钉接口调用失败 [{resp.status_code}]: {message} (code={code})",
            code=str(code),
        )

    return resp.json() if resp.text.strip() else {}


# ── DingTalk contact APIs (OAPI) ───────────────────────────────────────

def _text(value: Any) -> str:
    return str(value or "").strip()


async def _oapi_post(
    client: httpx.AsyncClient,
    path: str,
    token: str,
    body: dict[str, Any],
    *,
    tries: int = 4,
) -> dict[str, Any]:
    last_error: Exception | None = None
    current_token = token
    for attempt in range(tries):
        try:
            resp = await client.post(
                f"{_OAPI_BASE_URL}{path}",
                params={"access_token": current_token},
                json=body,
            )
            payload = resp.json()
            errcode = payload.get("errcode")
            if errcode == 0:
                return payload
            code = str(errcode or "")
            message = str(payload.get("errmsg") or payload)
            if code in {"40014", "42001"}:
                _clear_access_token_cache()
                current_token = await get_access_token()
            last_error = DingTalkApiError(
                f"钉钉通讯录接口调用失败 {path}: {message}",
                code=code,
            )
        except Exception as exc:
            last_error = exc
        await asyncio.sleep(0.25 * (attempt + 1))
    raise last_error or DingTalkApiError(f"钉钉通讯录接口调用失败 {path}")


async def _collect_contact_department_ids(client: httpx.AsyncClient, token: str) -> list[int]:
    seen: set[int] = set()
    queue: list[int] = [1]
    ordered: list[int] = []
    while queue:
        batch: list[int] = []
        while queue and len(batch) < _CONTACT_DEPT_CONCURRENCY:
            dept_id = int(queue.pop(0))
            if dept_id in seen:
                continue
            seen.add(dept_id)
            ordered.append(dept_id)
            batch.append(dept_id)
        if not batch:
            continue

        async def one(dept_id: int) -> tuple[int, list[int], str | None]:
            try:
                data = await _oapi_post(client, "/topapi/v2/department/listsubid", token, {"dept_id": dept_id})
                children = [int(item) for item in (data.get("result") or {}).get("dept_id_list") or []]
                return dept_id, children, None
            except Exception as exc:
                return dept_id, [], str(exc)

        results = await asyncio.gather(*(one(dept_id) for dept_id in batch))
        for dept_id, children, error in results:
            if error:
                logger.warning("failed to list DingTalk child departments for dept_id=%s: %s", dept_id, error)
            for child in children:
                if child not in seen and child not in queue:
                    queue.append(child)
    return ordered


def _merge_contact_user(users: dict[str, dict[str, Any]], row: dict[str, Any], seen_dept_id: int) -> None:
    userid = _text(row.get("userid"))
    if not userid:
        return
    current = users.setdefault(userid, {"userid": userid, "seen_dept_ids": []})
    if seen_dept_id not in current["seen_dept_ids"]:
        current["seen_dept_ids"].append(seen_dept_id)
    for key in [
        "name",
        "job_number",
        "title",
        "mobile",
        "active",
        "dept_id_list",
        "unionid",
        "email",
        "org_email",
        "remark",
        "state_code",
        "telephone",
        "work_place",
        "extension",
        "exclusive_account",
        "manager_userid",
    ]:
        value = row.get(key)
        if value in (None, "", []):
            continue
        if current.get(key) in (None, "", []):
            current[key] = value
        elif key == "dept_id_list" and isinstance(value, list):
            current[key] = list(dict.fromkeys([*(current.get(key) or []), *value]))


async def _collect_contact_users_by_job_number(
    client: httpx.AsyncClient,
    token: str,
    departments: list[int],
) -> dict[str, dict[str, Any]]:
    users_by_userid: dict[str, dict[str, Any]] = {}
    sem = asyncio.Semaphore(_CONTACT_USER_CONCURRENCY)

    async def list_dept_users(dept_id: int) -> None:
        cursor = 0
        async with sem:
            while True:
                try:
                    data = await _oapi_post(
                        client,
                        "/topapi/v2/user/list",
                        token,
                        {
                            "dept_id": dept_id,
                            "cursor": cursor,
                            "size": 100,
                            "contain_access_limit": True,
                            "language": "zh_CN",
                        },
                    )
                except Exception as exc:
                    logger.warning("failed to list DingTalk users for dept_id=%s cursor=%s: %s", dept_id, cursor, exc)
                    break
                result = data.get("result") or {}
                for row in result.get("list") or []:
                    if isinstance(row, dict):
                        _merge_contact_user(users_by_userid, row, dept_id)
                if result.get("has_more"):
                    cursor = int(result.get("next_cursor") or 0)
                else:
                    break

    await asyncio.gather(*(list_dept_users(dept_id) for dept_id in departments))

    users_by_job_number: dict[str, dict[str, Any]] = {}
    for row in users_by_userid.values():
        job_number = _text(row.get("job_number"))
        name = _text(row.get("name"))
        if not job_number or not name:
            continue
        current = users_by_job_number.get(job_number)
        if current is None or (not current.get("active") and row.get("active")):
            users_by_job_number[job_number] = row
    return users_by_job_number


async def list_dingtalk_contact_users_by_job_number(
    *,
    refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """Return DingTalk contact users keyed by job_number, cached briefly."""
    now = datetime.now(timezone.utc)
    cached_expires = _CONTACT_DIRECTORY_CACHE.get("expires_at")
    cached_users = _CONTACT_DIRECTORY_CACHE.get("by_job_number")
    if (
        not refresh
        and isinstance(cached_expires, datetime)
        and cached_expires > now
        and isinstance(cached_users, dict)
    ):
        return cached_users

    async with _CONTACT_DIRECTORY_LOCK:
        cached_expires = _CONTACT_DIRECTORY_CACHE.get("expires_at")
        cached_users = _CONTACT_DIRECTORY_CACHE.get("by_job_number")
        if (
            not refresh
            and isinstance(cached_expires, datetime)
            and cached_expires > now
            and isinstance(cached_users, dict)
        ):
            return cached_users

        token = await get_access_token()
        started = datetime.now(timezone.utc)
        async with httpx.AsyncClient(timeout=30.0) as client:
            departments = await _collect_contact_department_ids(client, token)
            users = await _collect_contact_users_by_job_number(client, token, departments)
        _CONTACT_DIRECTORY_CACHE["by_job_number"] = users
        _CONTACT_DIRECTORY_CACHE["expires_at"] = datetime.now(timezone.utc) + timedelta(
            seconds=_CONTACT_DIRECTORY_TTL_SECONDS
        )
        logger.info(
            "refreshed DingTalk contact directory: departments=%s users_with_job_number=%s elapsed=%.2fs",
            len(departments),
            len(users),
            (datetime.now(timezone.utc) - started).total_seconds(),
        )
        return users


async def lookup_dingtalk_user_by_job_number(job_number: str) -> dict[str, Any] | None:
    code = _text(job_number)
    if not code:
        return None
    return (await list_dingtalk_contact_users_by_job_number()).get(code)


# ── badge configuration ────────────────────────────────────────────────

async def configure_corp_badge(
    *,
    code_identity: str = "DT_IDENTITY",
    status: str = "OPEN",
) -> dict:
    """POST /v1.0/badge/codes/corpInstances – 配置企业钉工牌."""
    settings = get_settings()
    return await _request(
        "POST",
        "/v1.0/badge/codes/corpInstances",
        json_body={
            "codeIdentity": code_identity,
            "corpId": settings.dingtalk_corp_id,
            "status": status,
        },
    )


async def create_badge_code(
    *,
    request_id: str,
    code_identity: str,
    user_identity: str,
    user_corp_relation_type: str = "INTERNAL_STAFF",
    status: str = "OPEN",
    code_value: str | None = None,
    code_value_type: str | None = None,
    gmt_expired: str | None = None,
    available_times: list[dict] | None = None,
    ext_info: dict[str, str] | None = None,
) -> dict:
    """POST /v1.0/badge/codes/userInstances – 创建钉工牌电子码."""
    settings = get_settings()
    body: dict[str, Any] = {
        "requestId": request_id,
        "codeIdentity": code_identity,
        "corpId": settings.dingtalk_corp_id,
        "userCorpRelationType": user_corp_relation_type,
        "userIdentity": user_identity,
        "status": status,
    }
    if code_value is not None:
        body["codeValue"] = code_value
    if code_value_type is not None:
        body["codeValueType"] = code_value_type
    if gmt_expired is not None:
        body["gmtExpired"] = gmt_expired
    if available_times is not None:
        body["availableTimes"] = available_times
    if ext_info is not None:
        body["extInfo"] = ext_info
    return await _request("POST", "/v1.0/badge/codes/userInstances", json_body=body)


async def update_badge_code(
    *,
    code_id: str,
    code_identity: str,
    user_identity: str,
    user_corp_relation_type: str = "INTERNAL_STAFF",
    status: str | None = None,
    code_value: str | None = None,
    gmt_expired: str | None = None,
    available_times: list[dict] | None = None,
    ext_info: dict[str, str] | None = None,
) -> dict:
    """PUT /v1.0/badge/codes/userInstances – 更新钉工牌电子码."""
    settings = get_settings()
    body: dict[str, Any] = {
        "codeId": code_id,
        "codeIdentity": code_identity,
        "corpId": settings.dingtalk_corp_id,
        "userCorpRelationType": user_corp_relation_type,
        "userIdentity": user_identity,
    }
    if status is not None:
        body["status"] = status
    if code_value is not None:
        body["codeValue"] = code_value
    if gmt_expired is not None:
        body["gmtExpired"] = gmt_expired
    if available_times is not None:
        body["availableTimes"] = available_times
    if ext_info is not None:
        body["extInfo"] = ext_info
    return await _request("PUT", "/v1.0/badge/codes/userInstances", json_body=body)


async def decode_badge_code(*, pay_code: str, code_identity: str) -> dict:
    """POST /v1.0/badge/codes/decode – 解码钉工牌电子码."""
    settings = get_settings()
    return await _request(
        "POST",
        "/v1.0/badge/codes/decode",
        json_body={
            "payCode": pay_code,
            "corpId": settings.dingtalk_corp_id,
            "codeIdentity": code_identity,
        },
    )


async def notify_badge_code_verify_result(
    *,
    code_identity: str,
    user_identity: str,
    user_corp_relation_type: str = "INTERNAL_STAFF",
    verify_event: str,
    verify_time: str,
    verify_location: str | None = None,
) -> dict:
    """POST /v1.0/badge/codes/verifyResults – 同步钉工牌码验证结果."""
    settings = get_settings()
    body: dict[str, Any] = {
        "codeIdentity": code_identity,
        "corpId": settings.dingtalk_corp_id,
        "userCorpRelationType": user_corp_relation_type,
        "userIdentity": user_identity,
        "verifyEvent": verify_event,
        "verifyTime": verify_time,
    }
    if verify_location is not None:
        body["verifyLocation"] = verify_location
    return await _request("POST", "/v1.0/badge/codes/verifyResults", json_body=body)


# ── DVI device APIs (/v1.0/dvi/) ──────────────────────────────────────

async def dvi_list_devices(
    *,
    max_results: int = 50,
    next_token: str = "",
    sn: str | None = None,
    team_code: str | None = None,
    user_id: str | None = None,
) -> dict:
    """GET /v1.0/dvi/devices – 查询设备列表."""
    params: dict[str, str] = {
        "maxResults": str(max_results),
    }
    if next_token:
        params["nextToken"] = next_token
    if sn:
        params["sn"] = sn
    if team_code:
        params["teamCode"] = team_code
    if user_id:
        params["userId"] = user_id
    return await _request("GET", "/v1.0/dvi/devices", params=params)


async def dvi_query_device_status(sn_list: list[str], device_type: str = "B1") -> dict:
    """POST /v1.0/dvi/device/status – 批量查询设备状态."""
    return await _request(
        "POST",
        "/v1.0/dvi/device/status",
        json_body={
            "deviceType": device_type,
            "snList": sn_list,
        },
    )


async def dvi_query_device_detail(sn_list: list[str], device_type: str = "B1") -> dict:
    """POST /v1.0/dvi/device/list – 批量查询设备详情."""
    return await _request(
        "POST",
        "/v1.0/dvi/device/list",
        json_body={
            "deviceType": device_type,
            "snList": sn_list,
        },
    )


async def dvi_list_audio_files(
    sn: str,
    *,
    device_type: str = "B1",
    max_results: int = 20,
    next_token: str = "",
    start_timestamp: int | None = None,
    end_timestamp: int | None = None,
) -> dict:
    """POST /v1.0/dvi/device/audio/list – 分页查询设备音频文件."""
    body: dict[str, Any] = {
        "deviceType": device_type,
        "sn": sn,
        "maxResults": max_results,
    }
    if next_token:
        body["nextToken"] = next_token
    if start_timestamp is not None:
        body["startTimestamp"] = start_timestamp
    if end_timestamp is not None:
        body["endTimestamp"] = end_timestamp
    return await _request("POST", "/v1.0/dvi/device/audio/list", json_body=body)


async def dvi_get_audio_file_info(file_id: str, device_type: str = "B1") -> dict:
    """POST /v1.0/dvi/device/audio/get – 获取音频文件信息."""
    return await _request(
        "POST",
        "/v1.0/dvi/device/audio/get",
        json_body={
            "deviceType": device_type,
            "fileId": file_id,
        },
    )


async def dvi_get_audio_download_url(file_id: str, device_type: str = "B1") -> dict:
    """POST /v1.0/dvi/device/audio/download – 获取音频文件下载地址."""
    return await _request(
        "POST",
        "/v1.0/dvi/device/audio/download",
        json_body={
            "deviceType": device_type,
            "fileId": file_id,
        },
    )


async def dvi_update_device_binding(
    *,
    action: str,
    sn: str,
    team_code: str,
    user_id: str,
) -> dict:
    """POST /v1.0/dvi/devices/binding/update – 设备绑定/解绑.

    action: "bind" | "unbind"
    """
    return await _request(
        "POST",
        "/v1.0/dvi/devices/binding/update",
        json_body={
            "action": action,
            "sn": sn,
            "teamCode": team_code,
            "userId": user_id,
        },
    )


async def dvi_control_recording(
    *,
    action: str,
    team_code: str,
    user_id: str,
    agree: bool = True,
) -> dict:
    """POST /v1.0/dvi/devices/recording/control – 控制录音.

    action: "start" | "stop"
    """
    return await _request(
        "POST",
        "/v1.0/dvi/devices/recording/control",
        json_body={
            "action": action,
            "agree": agree,
            "teamCode": team_code,
            "userId": user_id,
        },
    )


async def dvi_list_recording_durations(
    *,
    max_results: int = 50,
    next_token: str = "",
    sn: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    team_code: str | None = None,
    user_id: str | None = None,
) -> dict:
    """GET /v1.0/dvi/devices/recording-durations – 查询录音时长."""
    params: dict[str, str] = {
        "maxResults": str(max_results),
    }
    if next_token:
        params["nextToken"] = next_token
    if sn:
        params["sn"] = sn
    if start_time:
        params["startTime"] = start_time
    if end_time:
        params["endTime"] = end_time
    if team_code:
        params["teamCode"] = team_code
    if user_id:
        params["userId"] = user_id
    return await _request("GET", "/v1.0/dvi/devices/recording-durations", params=params)


async def dvi_list_teams(
    *,
    max_results: int = 50,
    next_token: str = "",
) -> dict:
    """GET /v1.0/dvi/teams – 查询团队列表."""
    params: dict[str, str] = {
        "maxResults": str(max_results),
    }
    if next_token:
        params["nextToken"] = next_token
    return await _request("GET", "/v1.0/dvi/teams", params=params)


# ── ASR ────────────────────────────────────────────────────────────────

async def dvi_submit_asr_task(
    *,
    union_id: str,
    space_id: str | None = None,
    dentry_id: str | None = None,
    biz_key: str | None = None,
    source_language: str | None = None,
) -> dict:
    """POST /v1.0/dvi/asr/create – 提交 ASR 任务."""
    body: dict[str, Any] = {"unionId": union_id}
    if space_id is not None:
        body["spaceId"] = space_id
    if dentry_id is not None:
        body["dentryId"] = dentry_id
    if biz_key is not None:
        body["bizKey"] = biz_key
    if source_language is not None:
        body["sourceLanguage"] = source_language
    return await _request("POST", "/v1.0/dvi/asr/create", json_body=body)


async def dvi_query_asr_result(
    *,
    task_id: str | None = None,
    union_id: str | None = None,
    max_results: int = 20,
    next_token: str = "",
) -> dict:
    """GET /v1.0/dvi/asr/query – 查询 ASR 结果."""
    params: dict[str, str] = {"maxResults": str(max_results)}
    if task_id:
        params["taskId"] = task_id
    if union_id:
        params["unionId"] = union_id
    if next_token:
        params["nextToken"] = next_token
    return await _request("GET", "/v1.0/dvi/asr/query", params=params)
