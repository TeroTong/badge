from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from threading import Lock
from typing import Any
from urllib.parse import urlencode
from urllib.parse import urlparse

import httpx

from smart_badge_api.core.config import get_settings

_ACCESS_TOKEN_CACHE: dict[str, dict[str, str | datetime | None]] = {}
_ACCESS_TOKEN_LOCK = Lock()
_JSAPI_TICKET_CACHE: dict[str, dict[str, str | datetime | None]] = {}
_JSAPI_TICKET_LOCK = Lock()
_INVALID_TOKEN_ERRCODES = {40014, 42001}

# 共享 httpx AsyncClient，避免每次调用都创建新连接池。
_HTTP_CLIENT: httpx.AsyncClient | None = None
_HTTP_CLIENT_LOCK: asyncio.Lock | None = None
# Token 刷新的 singleflight 锁（以 cache_key 为粒度）。
_ACCESS_TOKEN_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}
_JSAPI_TICKET_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}
_REFRESH_LOCK_REGISTRY_LOCK: asyncio.Lock | None = None


def _refresh_lock_registry_lock() -> asyncio.Lock:
    global _REFRESH_LOCK_REGISTRY_LOCK
    if _REFRESH_LOCK_REGISTRY_LOCK is None:
        _REFRESH_LOCK_REGISTRY_LOCK = asyncio.Lock()
    return _REFRESH_LOCK_REGISTRY_LOCK


async def _get_token_refresh_lock(cache_key: str) -> asyncio.Lock:
    async with _refresh_lock_registry_lock():
        lock = _ACCESS_TOKEN_REFRESH_LOCKS.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            _ACCESS_TOKEN_REFRESH_LOCKS[cache_key] = lock
        return lock


async def _get_ticket_refresh_lock(cache_key: str) -> asyncio.Lock:
    async with _refresh_lock_registry_lock():
        lock = _JSAPI_TICKET_REFRESH_LOCKS.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            _JSAPI_TICKET_REFRESH_LOCKS[cache_key] = lock
        return lock


class WecomConfigError(RuntimeError):
    pass


class WecomApiError(RuntimeError):
    def __init__(self, message: str, *, errcode: int | None = None) -> None:
        super().__init__(message)
        self.errcode = errcode


@dataclass(slots=True)
class WecomTenantConfig:
    id: str
    name: str
    corp_id: str
    agent_id: str
    agent_secret: str
    frontend_url: str
    callback_token: str | None = None
    callback_aes_key: str | None = None
    host: str | None = None
    is_default: bool = False
    api_base_url: str | None = None
    oauth_base_url: str | None = None

    @property
    def cache_key(self) -> str:
        return f"{self.corp_id}:{self.agent_id}"

    @property
    def resolved_api_base_url(self) -> str:
        return (self.api_base_url or get_settings().wecom_api_base_url).rstrip("/")

    @property
    def resolved_oauth_base_url(self) -> str:
        return (self.oauth_base_url or get_settings().wecom_oauth_base_url).rstrip("/")


@dataclass(slots=True)
class WecomMemberIdentity:
    userid: str
    name: str | None = None
    mobile: str | None = None


@dataclass(slots=True)
class WecomJsSdkSignature:
    timestamp: int
    nonceStr: str
    signature: str


def legacy_wecom_tenant_config() -> WecomTenantConfig | None:
    settings = get_settings()
    if not settings.wecom_enabled:
        return None
    return WecomTenantConfig(
        id="legacy",
        name="默认企业微信",
        corp_id=settings.wecom_corp_id.strip(),
        agent_id=settings.wecom_agent_id.strip(),
        agent_secret=settings.wecom_agent_secret.strip(),
        frontend_url=settings.frontend_url.strip().rstrip("/"),
        callback_token=settings.wecom_callback_token.strip() or None,
        callback_aes_key=settings.wecom_callback_aes_key.strip() or None,
        is_default=True,
        api_base_url=settings.wecom_api_base_url,
        oauth_base_url=settings.wecom_oauth_base_url,
    )


def ensure_wecom_enabled(tenant: WecomTenantConfig | None = None) -> WecomTenantConfig:
    resolved = tenant or legacy_wecom_tenant_config()
    if (
        resolved
        and resolved.corp_id.strip()
        and resolved.agent_id.strip()
        and resolved.agent_secret.strip()
    ):
        return resolved
    if tenant is not None:
        raise WecomConfigError("企业微信租户配置不完整，请检查 corp_id、agent_id 和 agent_secret")
    if get_settings().wecom_enabled:
        resolved = legacy_wecom_tenant_config()
        if resolved is not None:
            return resolved
    raise WecomConfigError("企业微信免密登录未配置，请设置 WECOM_CORP_ID、WECOM_AGENT_ID 和 WECOM_AGENT_SECRET")


def build_wecom_authorize_url(redirect_uri: str, *, state: str, tenant: WecomTenantConfig | None = None) -> str:
    resolved_tenant = ensure_wecom_enabled(tenant)
    params = urlencode(
        {
            "appid": resolved_tenant.corp_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "snsapi_base",
            "state": state,
            "agentid": resolved_tenant.agent_id,
        }
    )
    return f"{resolved_tenant.resolved_oauth_base_url}/connect/oauth2/authorize?{params}#wechat_redirect"


def _make_wecom_http_client() -> httpx.AsyncClient:
    # 保留工厂函数供测试/负载场景下创建临时客户端使用。
    # 企业微信接口必须使用服务器本机出口 IP，避免继承系统代理后命中不可控的中转出口。
    return httpx.AsyncClient(timeout=15.0, trust_env=False)


async def _get_shared_wecom_client() -> httpx.AsyncClient:
    """全局共享的 httpx AsyncClient，进程全局连接池复用。"""
    global _HTTP_CLIENT, _HTTP_CLIENT_LOCK
    if _HTTP_CLIENT_LOCK is None:
        _HTTP_CLIENT_LOCK = asyncio.Lock()
    if _HTTP_CLIENT is not None and not _HTTP_CLIENT.is_closed:
        return _HTTP_CLIENT
    async with _HTTP_CLIENT_LOCK:
        if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
            limits = httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=30.0)
            _HTTP_CLIENT = httpx.AsyncClient(timeout=15.0, trust_env=False, limits=limits)
        return _HTTP_CLIENT


async def close_shared_wecom_client() -> None:
    """供 FastAPI lifespan 关闭时调用。"""
    global _HTTP_CLIENT
    client = _HTTP_CLIENT
    _HTTP_CLIENT = None
    if client is not None and not client.is_closed:
        await client.aclose()


def _empty_cache_entry() -> dict[str, str | datetime | None]:
    return {"value": None, "expires_at": None}


def _clear_access_token_cache(tenant: WecomTenantConfig | None = None) -> None:
    with _ACCESS_TOKEN_LOCK:
        if tenant is None:
            _ACCESS_TOKEN_CACHE.clear()
            return
        _ACCESS_TOKEN_CACHE[tenant.cache_key] = _empty_cache_entry()


def _clear_jsapi_ticket_cache(tenant: WecomTenantConfig | None = None, ticket_type: str | None = None) -> None:
    with _JSAPI_TICKET_LOCK:
        if tenant is None:
            _JSAPI_TICKET_CACHE.clear()
            return
        cache_keys = [f"{tenant.cache_key}:{ticket_type}"] if ticket_type else [
            key for key in _JSAPI_TICKET_CACHE if key.startswith(f"{tenant.cache_key}:")
        ]
        for key in cache_keys:
            if key not in _JSAPI_TICKET_CACHE:
                continue
            _JSAPI_TICKET_CACHE[key] = _empty_cache_entry()


async def _get_access_token(tenant: WecomTenantConfig | None = None) -> str:
    resolved_tenant = ensure_wecom_enabled(tenant)
    now = datetime.now(timezone.utc)
    cache_entry = _ACCESS_TOKEN_CACHE.get(resolved_tenant.cache_key) or {}
    cached_value = cache_entry.get("value")
    cached_expires_at = cache_entry.get("expires_at")
    if isinstance(cached_value, str) and isinstance(cached_expires_at, datetime) and cached_expires_at > now:
        return cached_value

    refresh_lock = await _get_token_refresh_lock(resolved_tenant.cache_key)
    async with refresh_lock:
        # 双重检查：其他协程可能在等锁期间完成了刷新。
        cache_entry = _ACCESS_TOKEN_CACHE.get(resolved_tenant.cache_key) or {}
        cached_value = cache_entry.get("value")
        cached_expires_at = cache_entry.get("expires_at")
        if isinstance(cached_value, str) and isinstance(cached_expires_at, datetime) and cached_expires_at > now:
            return cached_value

        client = await _get_shared_wecom_client()
        response = await client.get(
            f"{resolved_tenant.resolved_api_base_url}/cgi-bin/gettoken",
            params={
                "corpid": resolved_tenant.corp_id,
                "corpsecret": resolved_tenant.agent_secret,
            },
        )
        response.raise_for_status()
        payload = response.json()
        errcode = int(payload.get("errcode") or 0)
        if errcode != 0:
            errmsg = str(payload.get("errmsg") or "unknown error")
            raise WecomApiError(f"企业微信 access_token 获取失败：{errmsg} (errcode={errcode})")

        access_token = str(payload.get("access_token") or "").strip()
        expires_in = max(int(payload.get("expires_in") or 7200), 120)
        if not access_token:
            raise WecomApiError("企业微信 access_token 返回为空")

        with _ACCESS_TOKEN_LOCK:
            _ACCESS_TOKEN_CACHE[resolved_tenant.cache_key] = {
                "value": access_token,
                "expires_at": now + timedelta(seconds=expires_in - 60),
            }

        return access_token


async def _request_wecom_api(
    method: str,
    path: str,
    params: dict[str, str] | None = None,
    *,
    tenant: WecomTenantConfig | None = None,
    json_body: dict[str, Any] | None = None,
    retry: bool = True,
) -> dict:
    resolved_tenant = ensure_wecom_enabled(tenant)
    access_token = await _get_access_token(resolved_tenant)
    request_params = dict(params or {})
    request_params["access_token"] = access_token

    client = await _get_shared_wecom_client()
    response = await client.request(
        method.upper(),
        f"{resolved_tenant.resolved_api_base_url}{path}",
        params=request_params,
        json=json_body,
    )
    response.raise_for_status()
    payload = response.json()
    errcode = int(payload.get("errcode") or 0)
    if errcode == 0:
        return payload

    if retry and errcode in _INVALID_TOKEN_ERRCODES:
        _clear_access_token_cache(resolved_tenant)
        return await _request_wecom_api(method, path, params, tenant=resolved_tenant, json_body=json_body, retry=False)

    errmsg = str(payload.get("errmsg") or "unknown error")
    raise WecomApiError(f"企业微信接口调用失败：{errmsg} (errcode={errcode})", errcode=errcode)


async def _call_wecom_api(
    path: str,
    params: dict[str, str],
    *,
    tenant: WecomTenantConfig | None = None,
    retry: bool = True,
) -> dict:
    return await _request_wecom_api("GET", path, params, tenant=tenant, retry=retry)


async def _post_wecom_api(
    path: str,
    params: dict[str, str],
    json_body: dict[str, Any],
    *,
    tenant: WecomTenantConfig | None = None,
    retry: bool = True,
) -> dict:
    return await _request_wecom_api("POST", path, params, tenant=tenant, json_body=json_body, retry=retry)


async def _get_jsapi_ticket(ticket_type: str = "config", tenant: WecomTenantConfig | None = None) -> str:
    if ticket_type not in {"config", "agent"}:
        raise WecomApiError(f"不支持的企业微信 ticket 类型：{ticket_type}")

    resolved_tenant = ensure_wecom_enabled(tenant)
    cache_key = f"{resolved_tenant.cache_key}:{ticket_type}"
    now = datetime.now(timezone.utc)
    cache_entry = _JSAPI_TICKET_CACHE.get(cache_key) or {}
    cached_value = cache_entry.get("value")
    cached_expires_at = cache_entry.get("expires_at")
    if isinstance(cached_value, str) and isinstance(cached_expires_at, datetime) and cached_expires_at > now:
        return cached_value

    refresh_lock = await _get_ticket_refresh_lock(cache_key)
    async with refresh_lock:
        cache_entry = _JSAPI_TICKET_CACHE.get(cache_key) or {}
        cached_value = cache_entry.get("value")
        cached_expires_at = cache_entry.get("expires_at")
        if isinstance(cached_value, str) and isinstance(cached_expires_at, datetime) and cached_expires_at > now:
            return cached_value

        if ticket_type == "config":
            payload = await _call_wecom_api("/cgi-bin/get_jsapi_ticket", {}, tenant=resolved_tenant)
        else:
            payload = await _call_wecom_api("/cgi-bin/ticket/get", {"type": "agent_config"}, tenant=resolved_tenant)

        ticket = str(payload.get("ticket") or "").strip()
        expires_in = max(int(payload.get("expires_in") or 7200), 120)
        if not ticket:
            raise WecomApiError("企业微信 jsapi_ticket 返回为空")

        with _JSAPI_TICKET_LOCK:
            _JSAPI_TICKET_CACHE[cache_key] = {
                "value": ticket,
                "expires_at": now + timedelta(seconds=expires_in - 60),
            }
        return ticket


def _normalize_signature_url(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    if not parsed.scheme or not parsed.netloc:
        raise WecomApiError("企业微信 JS-SDK 签名 URL 非法")
    return parsed._replace(fragment="").geturl()


def build_wecom_js_sdk_signature(
    ticket: str,
    url: str,
    *,
    timestamp: int | None = None,
    nonce_str: str | None = None,
) -> WecomJsSdkSignature:
    safe_url = _normalize_signature_url(url)
    safe_timestamp = timestamp or int(datetime.now(timezone.utc).timestamp())
    safe_nonce = nonce_str or secrets.token_urlsafe(12)
    raw = f"jsapi_ticket={ticket}&noncestr={safe_nonce}&timestamp={safe_timestamp}&url={safe_url}"
    signature = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return WecomJsSdkSignature(
        timestamp=safe_timestamp,
        nonceStr=safe_nonce,
        signature=signature,
    )


async def build_wecom_js_sdk_signature_for_url(
    url: str,
    tenant: WecomTenantConfig | None = None,
) -> WecomJsSdkSignature:
    ticket = await _get_jsapi_ticket("config", tenant=tenant)
    return build_wecom_js_sdk_signature(ticket, url)


async def fetch_wecom_member_identity(
    code: str,
    tenant: WecomTenantConfig | None = None,
) -> WecomMemberIdentity:
    clean_code = code.strip()
    if not clean_code:
        raise WecomApiError("企业微信登录 code 不能为空")

    login_payload = await _call_wecom_api("/cgi-bin/user/getuserinfo", {"code": clean_code}, tenant=tenant)
    userid = str(login_payload.get("UserId") or login_payload.get("userid") or "").strip()
    if not userid:
        raise WecomApiError("企业微信未返回成员 UserId，请确认当前应用为企业内部应用且成员已获得应用可见权限")

    profile_payload = await _call_wecom_api("/cgi-bin/user/get", {"userid": userid}, tenant=tenant)
    return WecomMemberIdentity(
        userid=str(profile_payload.get("userid") or userid).strip(),
        name=str(profile_payload.get("name") or "").strip() or None,
        mobile=str(profile_payload.get("mobile") or "").strip() or None,
    )


async def fetch_wecom_app_menu(
    agent_id: str | None = None,
    tenant: WecomTenantConfig | None = None,
) -> dict[str, Any]:
    resolved_tenant = ensure_wecom_enabled(tenant)
    resolved_agent_id = (agent_id or resolved_tenant.agent_id).strip()
    if not resolved_agent_id:
        raise WecomConfigError("企业微信应用菜单未配置 agent_id，请先设置 WECOM_AGENT_ID")
    payload = await _call_wecom_api("/cgi-bin/menu/get", {"agentid": resolved_agent_id}, tenant=resolved_tenant)
    menu = payload.get("menu")
    if isinstance(menu, dict):
        return menu
    if "button" in payload:
        return {"button": payload.get("button") or []}
    return {"button": []}


async def publish_wecom_app_menu(
    menu: dict[str, Any],
    agent_id: str | None = None,
    tenant: WecomTenantConfig | None = None,
) -> dict[str, Any]:
    resolved_tenant = ensure_wecom_enabled(tenant)
    resolved_agent_id = (agent_id or resolved_tenant.agent_id).strip()
    if not resolved_agent_id:
        raise WecomConfigError("企业微信应用菜单未配置 agent_id，请先设置 WECOM_AGENT_ID")
    await _post_wecom_api("/cgi-bin/menu/create", {"agentid": resolved_agent_id}, menu, tenant=resolved_tenant)
    return menu


async def delete_wecom_app_menu(
    agent_id: str | None = None,
    tenant: WecomTenantConfig | None = None,
) -> None:
    resolved_tenant = ensure_wecom_enabled(tenant)
    resolved_agent_id = (agent_id or resolved_tenant.agent_id).strip()
    if not resolved_agent_id:
        raise WecomConfigError("企业微信应用菜单未配置 agent_id，请先设置 WECOM_AGENT_ID")
    await _call_wecom_api("/cgi-bin/menu/delete", {"agentid": resolved_agent_id}, tenant=resolved_tenant)


async def send_wecom_text_message(
    *,
    to_user: str,
    content: str,
    tenant: WecomTenantConfig | None = None,
    enable_duplicate_check: bool = True,
    duplicate_check_interval: int = 1800,
) -> dict[str, Any]:
    resolved_tenant = ensure_wecom_enabled(tenant)
    target_user = str(to_user or "").strip()
    if not target_user:
        raise WecomConfigError("企业微信消息接收人 UserId 不能为空")
    clean_content = str(content or "").strip()
    if not clean_content:
        raise WecomConfigError("企业微信消息内容不能为空")
    agent_id_payload: int | str = (
        int(resolved_tenant.agent_id)
        if str(resolved_tenant.agent_id).strip().isdigit()
        else resolved_tenant.agent_id
    )
    return await _post_wecom_api(
        "/cgi-bin/message/send",
        {},
        {
            "touser": target_user,
            "msgtype": "text",
            "agentid": agent_id_payload,
            "text": {"content": clean_content},
            "safe": 0,
            "enable_duplicate_check": 1 if enable_duplicate_check else 0,
            "duplicate_check_interval": max(int(duplicate_check_interval or 0), 0),
        },
        tenant=resolved_tenant,
    )


async def send_wecom_textcard_message(
    *,
    to_user: str,
    title: str,
    description: str,
    url: str,
    btn_text: str = "查看详情",
    tenant: WecomTenantConfig | None = None,
) -> dict[str, Any]:
    resolved_tenant = ensure_wecom_enabled(tenant)
    target_user = str(to_user or "").strip()
    if not target_user:
        raise WecomConfigError("企业微信消息接收人 UserId 不能为空")
    clean_title = str(title or "").strip()
    if not clean_title:
        raise WecomConfigError("企业微信卡片标题不能为空")
    clean_description = str(description or "").strip()
    if not clean_description:
        raise WecomConfigError("企业微信卡片描述不能为空")
    clean_url = str(url or "").strip()
    if not clean_url:
        raise WecomConfigError("企业微信卡片跳转 URL 不能为空")
    agent_id_payload: int | str = (
        int(resolved_tenant.agent_id)
        if str(resolved_tenant.agent_id).strip().isdigit()
        else resolved_tenant.agent_id
    )
    return await _post_wecom_api(
        "/cgi-bin/message/send",
        {},
        {
            "touser": target_user,
            "msgtype": "textcard",
            "agentid": agent_id_payload,
            "textcard": {
                "title": clean_title,
                "description": clean_description,
                "url": clean_url,
                "btntxt": str(btn_text or "查看详情").strip() or "查看详情",
            },
            "safe": 0,
            "enable_duplicate_check": 1,
            "duplicate_check_interval": 1800,
        },
        tenant=resolved_tenant,
    )


async def send_wecom_button_interaction_card(
    *,
    to_user: str,
    title: str,
    description: str,
    task_id: str,
    buttons: list[dict[str, str | int]],
    main_title_desc: str | None = None,
    source_desc: str = "朗姿智能工牌",
    horizontal_content_list: list[dict[str, str | int]] | None = None,
    tenant: WecomTenantConfig | None = None,
) -> dict[str, Any]:
    resolved_tenant = ensure_wecom_enabled(tenant)
    target_user = str(to_user or "").strip()
    if not target_user:
        raise WecomConfigError("企业微信消息接收人 UserId 不能为空")
    clean_title = str(title or "").strip()
    if not clean_title:
        raise WecomConfigError("企业微信交互卡片标题不能为空")
    clean_description = str(description or "").strip()
    if not clean_description:
        raise WecomConfigError("企业微信交互卡片描述不能为空")
    clean_task_id = str(task_id or "").strip()
    if not clean_task_id:
        raise WecomConfigError("企业微信交互卡片 task_id 不能为空")

    button_list: list[dict[str, str | int]] = []
    for button in buttons:
        text = str(button.get("text") or "").strip()
        key = str(button.get("key") or "").strip()
        if not text or not key:
            continue
        item: dict[str, str | int] = {"text": text, "key": key, "type": 0}
        style = button.get("style")
        if isinstance(style, int) and 1 <= style <= 4:
            item["style"] = style
        button_list.append(item)
    if not button_list:
        raise WecomConfigError("企业微信交互卡片按钮不能为空")

    horizontal_items: list[dict[str, str | int]] = []
    for item in horizontal_content_list or []:
        keyname = str(item.get("keyname") or "").strip()
        value = str(item.get("value") or "").strip()
        if not keyname or not value:
            continue
        horizontal_item: dict[str, str | int] = {"keyname": keyname, "value": value}
        item_type = item.get("type")
        if isinstance(item_type, int) and item_type >= 0:
            horizontal_item["type"] = item_type
        horizontal_items.append(horizontal_item)

    agent_id_payload: int | str = (
        int(resolved_tenant.agent_id)
        if str(resolved_tenant.agent_id).strip().isdigit()
        else resolved_tenant.agent_id
    )
    template_card: dict[str, Any] = {
        "card_type": "button_interaction",
        "source": {"desc": source_desc},
        "main_title": {
            "title": clean_title,
            "desc": str(main_title_desc or "").strip() or None,
        },
        "sub_title_text": clean_description,
        "button_list": button_list,
        "task_id": clean_task_id,
    }
    if horizontal_items:
        template_card["horizontal_content_list"] = horizontal_items
    template_card["main_title"] = {
        key: value for key, value in template_card["main_title"].items() if value
    }
    return await _post_wecom_api(
        "/cgi-bin/message/send",
        {},
        {
            "touser": target_user,
            "msgtype": "template_card",
            "agentid": agent_id_payload,
            "template_card": template_card,
            "enable_duplicate_check": 1,
            "duplicate_check_interval": 1800,
        },
        tenant=resolved_tenant,
    )


async def update_wecom_template_card_button(
    *,
    to_user: str,
    response_code: str,
    replace_name: str,
    tenant: WecomTenantConfig | None = None,
) -> dict[str, Any]:
    resolved_tenant = ensure_wecom_enabled(tenant)
    target_user = str(to_user or "").strip()
    if not target_user:
        raise WecomConfigError("企业微信消息接收人 UserId 不能为空")
    clean_response_code = str(response_code or "").strip()
    if not clean_response_code:
        raise WecomConfigError("企业微信卡片 response_code 不能为空")
    clean_replace_name = str(replace_name or "").strip()
    if not clean_replace_name:
        raise WecomConfigError("企业微信卡片按钮替换文案不能为空")
    agent_id_payload: int | str = (
        int(resolved_tenant.agent_id)
        if str(resolved_tenant.agent_id).strip().isdigit()
        else resolved_tenant.agent_id
    )
    return await _post_wecom_api(
        "/cgi-bin/message/update_template_card",
        {},
        {
            "userids": [target_user],
            "agentid": agent_id_payload,
            "response_code": clean_response_code,
            "button": {"replace_name": clean_replace_name[:20]},
        },
        tenant=resolved_tenant,
    )


async def update_wecom_button_interaction_card(
    *,
    to_user: str,
    response_code: str,
    title: str,
    description: str,
    task_id: str,
    buttons: list[dict[str, str | int]],
    main_title_desc: str | None = None,
    source_desc: str = "朗姿智能工牌",
    horizontal_content_list: list[dict[str, str | int]] | None = None,
    tenant: WecomTenantConfig | None = None,
) -> dict[str, Any]:
    resolved_tenant = ensure_wecom_enabled(tenant)
    target_user = str(to_user or "").strip()
    if not target_user:
        raise WecomConfigError("企业微信消息接收人 UserId 不能为空")
    clean_response_code = str(response_code or "").strip()
    if not clean_response_code:
        raise WecomConfigError("企业微信卡片 response_code 不能为空")
    clean_title = str(title or "").strip()
    if not clean_title:
        raise WecomConfigError("企业微信交互卡片标题不能为空")
    clean_description = str(description or "").strip()
    if not clean_description:
        raise WecomConfigError("企业微信交互卡片描述不能为空")
    clean_task_id = str(task_id or "").strip()
    if not clean_task_id:
        raise WecomConfigError("企业微信交互卡片 task_id 不能为空")

    button_list: list[dict[str, str | int]] = []
    for button in buttons:
        text = str(button.get("text") or "").strip()
        key = str(button.get("key") or "").strip()
        if not text or not key:
            continue
        item: dict[str, str | int] = {"text": text, "key": key, "type": 0}
        style = button.get("style")
        if isinstance(style, int) and 1 <= style <= 4:
            item["style"] = style
        button_list.append(item)
    if not button_list:
        raise WecomConfigError("企业微信交互卡片按钮不能为空")

    horizontal_items: list[dict[str, str | int]] = []
    for item in horizontal_content_list or []:
        keyname = str(item.get("keyname") or "").strip()
        value = str(item.get("value") or "").strip()
        if not keyname or not value:
            continue
        horizontal_item: dict[str, str | int] = {"keyname": keyname, "value": value}
        item_type = item.get("type")
        if isinstance(item_type, int) and item_type >= 0:
            horizontal_item["type"] = item_type
        horizontal_items.append(horizontal_item)

    agent_id_payload: int | str = (
        int(resolved_tenant.agent_id)
        if str(resolved_tenant.agent_id).strip().isdigit()
        else resolved_tenant.agent_id
    )
    template_card: dict[str, Any] = {
        "card_type": "button_interaction",
        "source": {"desc": source_desc},
        "main_title": {
            "title": clean_title,
            "desc": str(main_title_desc or "").strip() or None,
        },
        "sub_title_text": clean_description,
        "button_list": button_list,
        "task_id": clean_task_id,
    }
    if horizontal_items:
        template_card["horizontal_content_list"] = horizontal_items
    template_card["main_title"] = {
        key: value for key, value in template_card["main_title"].items() if value
    }
    return await _post_wecom_api(
        "/cgi-bin/message/update_template_card",
        {},
        {
            "userids": [target_user],
            "agentid": agent_id_payload,
            "response_code": clean_response_code,
            "template_card": template_card,
        },
        tenant=resolved_tenant,
    )
