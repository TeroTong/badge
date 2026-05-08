"""FastAPI 依赖项与认证工具。"""

from __future__ import annotations

from fastapi import Depends, HTTPException, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.data_scope import build_permission_scope
from smart_badge_api.core.permissions import PermissionScope, permission_role_level
from smart_badge_api.core.security import decode_access_token
from smart_badge_api.db.models import User
from smart_badge_api.db.session import get_db

_bearer = HTTPBearer(auto_error=False)


async def get_user_from_token(token: str, db: AsyncSession) -> User | None:
    user_id = decode_access_token(token)
    if user_id is None:
        return None

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


def get_websocket_token(websocket: WebSocket) -> str | None:
    auth_header = websocket.headers.get("authorization")
    if auth_header:
        scheme, _, value = auth_header.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value

    return websocket.query_params.get("token")


async def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    """从 `Authorization: Bearer <token>` 解析当前用户。"""
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未提供认证令牌")

    user = await get_user_from_token(cred.credentials, db)
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
