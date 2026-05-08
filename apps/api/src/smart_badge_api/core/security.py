"""JWT 令牌与密码哈希工具。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from smart_badge_api.core.config import get_settings

try:
    import bcrypt

    _hashpw = bcrypt.hashpw
    _gensalt = bcrypt.gensalt
    _checkpw = bcrypt.checkpw
except (ImportError, AttributeError):
    import bcrypt._bcrypt as _bcrypt

    _hashpw = _bcrypt.hashpw
    _gensalt = _bcrypt.gensalt
    _checkpw = _bcrypt.checkpw

ALGORITHM = "HS256"


def hash_password(plain: str) -> str:
    return _hashpw(plain.encode(), _gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return _checkpw(plain.encode(), hashed.encode())


def create_access_token(subject: str) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": subject, "exp": expire, "type": "access"}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def create_refresh_token(subject: str) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    payload = {"sub": subject, "exp": expire, "type": "refresh"}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> str | None:
    """返回 subject (user id) 或 None（令牌无效时）。"""
    try:
        payload = jwt.decode(token, get_settings().secret_key, algorithms=[ALGORITHM])
        if payload.get("type", "access") != "access":
            return None
        return payload.get("sub")
    except JWTError:
        return None


def decode_refresh_token(token: str) -> str | None:
    """返回 subject (user id) 或 None（令牌无效时）。"""
    try:
        payload = jwt.decode(token, get_settings().secret_key, algorithms=[ALGORITHM])
        if payload.get("type") != "refresh":
            return None
        return payload.get("sub")
    except JWTError:
        return None
