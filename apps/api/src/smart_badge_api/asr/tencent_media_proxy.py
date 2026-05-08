from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import quote

from smart_badge_api.core.config import get_settings


def _urlsafe_b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(f"{value}{padding}".encode("ascii"), altchars=b"-_", validate=True)
    except Exception as exc:  # pragma: no cover - defensive path
        raise ValueError("无效的媒体访问令牌") from exc
    if _urlsafe_b64encode(decoded) != value:
        raise ValueError("无效的媒体访问令牌")
    return decoded


def _resolve_upload_relative_path(file_path: Path) -> str:
    settings = get_settings()
    resolved = file_path.resolve()
    try:
        return str(resolved.relative_to(settings.upload_path.resolve()))
    except ValueError as exc:
        raise ValueError("腾讯云 ASR 仅支持访问上传目录中的音频文件") from exc


def build_tencent_media_token(
    file_path: str | Path,
    *,
    filename: str | None = None,
    ttl_seconds: int | None = None,
) -> str:
    settings = get_settings()
    expires_at = int(time.time()) + int(ttl_seconds or settings.tencent_asr_public_media_ttl_seconds)
    payload = {
        "stored": _resolve_upload_relative_path(Path(file_path)),
        "exp": expires_at,
    }
    if filename:
        payload["filename"] = filename

    payload_raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(
        settings.secret_key.encode("utf-8"),
        payload_raw,
        hashlib.sha256,
    ).digest()
    return f"{_urlsafe_b64encode(payload_raw)}.{_urlsafe_b64encode(signature)}"


def resolve_tencent_media_token(token: str) -> tuple[Path, str | None]:
    settings = get_settings()
    try:
        payload_b64, signature_b64 = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("无效的媒体访问令牌") from exc

    payload_raw = _urlsafe_b64decode(payload_b64)
    expected_signature = hmac.new(
        settings.secret_key.encode("utf-8"),
        payload_raw,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected_signature, _urlsafe_b64decode(signature_b64)):
        raise ValueError("媒体访问令牌签名校验失败")

    try:
        payload = json.loads(payload_raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("媒体访问令牌内容无效") from exc

    expires_at = int(payload.get("exp") or 0)
    if expires_at <= int(time.time()):
        raise ValueError("媒体访问令牌已过期")

    stored = Path(str(payload.get("stored") or ""))
    if stored.is_absolute() or ".." in stored.parts:
        raise ValueError("媒体访问路径非法")

    resolved = (settings.upload_path / stored).resolve()
    try:
        resolved.relative_to(settings.upload_path.resolve())
    except ValueError as exc:
        raise ValueError("媒体访问路径越界") from exc

    filename = str(payload.get("filename") or "").strip() or None
    return resolved, filename


def build_tencent_media_url(
    file_path: str | Path,
    *,
    filename: str | None = None,
    ttl_seconds: int | None = None,
) -> str:
    settings = get_settings()
    base_url = settings.tencent_asr_public_media_base_url.strip() or settings.frontend_url
    return f"{base_url.rstrip('/')}{build_tencent_media_path(file_path, filename=filename, ttl_seconds=ttl_seconds)}"


def build_tencent_media_path(
    file_path: str | Path,
    *,
    filename: str | None = None,
    ttl_seconds: int | None = None,
) -> str:
    settings = get_settings()
    token = build_tencent_media_token(file_path, filename=filename, ttl_seconds=ttl_seconds)
    prefix = settings.api_v1_prefix.rstrip("/")
    route_path = f"{prefix}/asr/tencent-media" if prefix else "/asr/tencent-media"
    return f"{route_path}?token={quote(token, safe='')}"
