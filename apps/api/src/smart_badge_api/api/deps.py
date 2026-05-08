"""FastAPI 依赖项与认证工具。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from typing import Any

from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.data_scope import build_permission_scope
from smart_badge_api.core.config import get_settings
from smart_badge_api.core.permissions import PermissionScope, permission_role_level
from smart_badge_api.core.security import decode_access_token
from smart_badge_api.db.models import User
from smart_badge_api.db.session import get_db

_bearer = HTTPBearer(auto_error=False)
_user_token_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_user_token_cache_lock = asyncio.Lock()
_user_token_load_locks: dict[str, asyncio.Lock] = {}


def _token_cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _user_cache_payload(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "hashed_password": user.hashed_password,
        "display_name": user.display_name,
        "staff_id": user.staff_id,
        "role": user.role,
        "hospital_code": user.hospital_code,
        "hospital_name": user.hospital_name,
        "is_active": user.is_active,
        "last_login_at": user.last_login_at,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


def _user_from_cache_payload(payload: dict[str, Any]) -> User:
    return User(**payload)


async def _get_cached_user(token: str) -> User | None:
    settings = get_settings()
    ttl = max(0.0, settings.auth_user_cache_ttl_seconds)
    if ttl <= 0:
        return None
    key = _token_cache_key(token)
    now = time.monotonic()
    async with _user_token_cache_lock:
        cached = _user_token_cache.get(key)
        if cached is None:
            return None
        expires_at, payload = cached
        if expires_at <= now:
            _user_token_cache.pop(key, None)
            return None
        _user_token_cache.move_to_end(key)
        return _user_from_cache_payload(payload)


async def _set_cached_user(token: str, user: User) -> None:
    settings = get_settings()
    ttl = max(0.0, settings.auth_user_cache_ttl_seconds)
    if ttl <= 0:
        return
    key = _token_cache_key(token)
    max_items = max(1, settings.auth_user_cache_max_items)
    async with _user_token_cache_lock:
        _user_token_cache[key] = (time.monotonic() + ttl, _user_cache_payload(user))
        _user_token_cache.move_to_end(key)
        while len(_user_token_cache) > max_items:
            _user_token_cache.popitem(last=False)


def _should_use_cached_user(request: Request) -> bool:
    if request.method.upper() not in {"GET", "HEAD"}:
        return False

    settings = get_settings()
    api_prefix = settings.api_v1_prefix.rstrip("/")
    path = request.url.path.rstrip("/")
    hot_paths = {
        f"{api_prefix}/customers",
        f"{api_prefix}/recordings",
        f"{api_prefix}/visits",
        f"{api_prefix}/visit-orders",
        f"{api_prefix}/transcripts",
        f"{api_prefix}/staff",
        f"{api_prefix}/positions",
        f"{api_prefix}/sap-hana-visit-orders",
        f"{api_prefix}/hotwords/groups",
        f"{api_prefix}/rule-groups",
        f"{api_prefix}/risk-rules",
        f"{api_prefix}/quality/dimensions",
        f"{api_prefix}/analysis/results",
        f"{api_prefix}/sap-push-monitoring/logs",
        f"{api_prefix}/dashboard",
        f"{api_prefix}/sap-push-monitoring/overview",
        f"{api_prefix}/asr-monitoring/overview",
        f"{api_prefix}/dingtalk/devices",
        f"{api_prefix}/account/managed-badges",
        f"{api_prefix}/account/my-badge",
    }
    if path in hot_paths:
        return True
    parts = path[len(api_prefix):].strip("/").split("/") if path.startswith(api_prefix) else []
    if len(parts) == 2 and parts[0] in {"customers", "staff"}:
        return True
    if len(parts) == 3 and parts[0] == "customers" and parts[2] in {"detail", "merged-analysis", "tag-completion", "visit-orders"}:
        return True
    if len(parts) == 3 and parts[0] == "recordings" and parts[1] == "archive":
        return True
    return False


async def get_user_from_token(token: str, db: AsyncSession, *, use_cache: bool = True) -> User | None:
    user_id = decode_access_token(token)
    if user_id is None:
        return None

    if not use_cache:
        user = await db.get(User, user_id)
        if user is None or not user.is_active:
            return None
        return user

    cache_key = _token_cache_key(token)
    cached_user = await _get_cached_user(token)
    if cached_user is not None:
        return cached_user

    async with _user_token_cache_lock:
        load_lock = _user_token_load_locks.get(cache_key)
        if load_lock is None:
            load_lock = asyncio.Lock()
            _user_token_load_locks[cache_key] = load_lock

    try:
        async with load_lock:
            cached_user = await _get_cached_user(token)
            if cached_user is not None:
                return cached_user

            user = await db.get(User, user_id)
            if user is None or not user.is_active:
                return None
            await _set_cached_user(token, user)
            return user
    finally:
        async with _user_token_cache_lock:
            current_lock = _user_token_load_locks.get(cache_key)
            if current_lock is load_lock and not load_lock.locked():
                _user_token_load_locks.pop(cache_key, None)


def get_websocket_token(websocket: WebSocket) -> str | None:
    auth_header = websocket.headers.get("authorization")
    if auth_header:
        scheme, _, value = auth_header.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value

    return websocket.query_params.get("token")


async def get_current_user(
    request: Request,
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    """从 `Authorization: Bearer <token>` 解析当前用户。"""
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未提供认证令牌")

    user = await get_user_from_token(cred.credentials, db, use_cache=_should_use_cached_user(request))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效、已过期或用户不可用")
    return user


# ── 角色权限 ──────────────────────────────────────


ROLE_HIERARCHY: dict[str, int] = {
    "staff": 10,
    "hospital_admin": 30,
    "system_admin": 90,
    "super_admin": 100,
}


def require_roles(*allowed_roles: str):
    """创建一个 FastAPI 依赖项，要求当前用户角色在允许列表中。

    用法：
        @router.post("/...", dependencies=[Depends(require_roles("admin", "manager"))])
    """

    async def _check(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed_roles:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"权限不足，需要角色: {', '.join(allowed_roles)}",
            )
        return user

    return _check


def require_min_role(min_role: str):
    async def _check(user: User = Depends(get_current_user)) -> User:
        if permission_role_level(user.role) < permission_role_level(min_role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"权限不足，需要至少 {min_role} 角色")
        return user

    return _check


async def get_current_permission_scope(
    user: User = Depends(get_current_user),
) -> PermissionScope:
    return await build_permission_scope(user)


# 常用快捷依赖
require_super_admin = require_roles("super_admin")
require_system_admin_or_above = require_min_role("system_admin")
require_hospital_admin_or_above = require_min_role("hospital_admin")
require_any_role = require_min_role("staff")
