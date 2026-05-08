from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import string
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx

from smart_badge_api.core.config import get_settings

logger = logging.getLogger(__name__)

# 共享 httpx AsyncClient（按超时配置创建一次，连接池复用）。
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
            settings = get_settings()
            timeout = httpx.Timeout(settings.xfyun_asr_request_timeout_seconds, connect=30.0)
            limits = httpx.Limits(max_keepalive_connections=10, max_connections=30, keepalive_expiry=30.0)
            _HTTP_CLIENT = httpx.AsyncClient(timeout=timeout, limits=limits)
        return _HTTP_CLIENT


async def close_shared_xfyun_client() -> None:
    global _HTTP_CLIENT
    client = _HTTP_CLIENT
    _HTTP_CLIENT = None
    if client is not None and not client.is_closed:
        await client.aclose()

_MAX_FILE_SIZE_BYTES = 500 * 1024 * 1024
_SIGNATURE_ALPHABET = string.ascii_letters + string.digits
_CACHE_VERSION = 1
_FAIL_TYPE_LABELS = {
    0: "音频正常执行",
    1: "音频上传失败",
    2: "音频转码失败",
    3: "音频识别失败",
    4: "音频时长超限",
    5: "音频校验失败",
    6: "静音文件",
    7: "翻译失败",
    8: "账号无翻译权限",
    9: "转写质检失败",
    10: "转写质检未匹配出关键词",
    11: "未开通对应能力",
    12: "音频语种分析失败",
    99: "其他异常",
}


class XfyunAsrError(RuntimeError):
    """科大讯飞录音文件转写大模型调用失败。"""


def _normalize_base_url(value: str) -> str:
    text = value.strip()
    if not text:
        return "https://office-api-ist-dx.iflyaisol.com"
    parsed = urlparse(text if "://" in text else f"https://{text}")
    scheme = parsed.scheme or "https"
    host = parsed.netloc or parsed.path
    if not host:
        return "https://office-api-ist-dx.iflyaisol.com"
    return f"{scheme}://{host}".rstrip("/")


def _now_string() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _generate_signature_random(length: int = 16) -> str:
    return "".join(secrets.choice(_SIGNATURE_ALPHABET) for _ in range(max(length, 16)))


def _stringify_bool(value: bool) -> str:
    return "true" if value else "false"


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
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


def _normalize_speaker_label(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    return f"speaker_{text}" if text.isdigit() else text


def _build_signature(params: dict[str, Any], access_key_secret: str) -> str:
    items = sorted(
        (key, value)
        for key, value in params.items()
        if key != "signature" and value is not None and str(value) != ""
    )
    base_string = "&".join(
        f"{quote_plus(str(key), safe='')}={quote_plus(str(value), safe='')}"
        for key, value in items
    )
    digest = hmac.new(
        access_key_secret.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _validate_runtime_prerequisites() -> None:
    settings = get_settings()
    if not settings.xfyun_asr_app_id.strip():
        raise XfyunAsrError("未配置讯飞 ASR AppID，请设置 XFYUN_ASR_APP_ID")
    if not settings.xfyun_asr_access_key_id.strip():
        raise XfyunAsrError("未配置讯飞 ASR accessKeyId，请设置 XFYUN_ASR_ACCESS_KEY_ID")
    if not settings.xfyun_asr_access_key_secret.strip():
        raise XfyunAsrError("未配置讯飞 ASR accessKeySecret，请设置 XFYUN_ASR_ACCESS_KEY_SECRET")


def _cache_root() -> Path:
    cache_root = get_settings().asr_runtime_path / "xfyun_cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    return cache_root


def _compute_audio_sha256(audio_path: Path) -> str:
    digest = hashlib.sha256()
    with audio_path.open("rb") as file_obj:
        while True:
            chunk = file_obj.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _cache_settings_snapshot(audio_path: Path) -> dict[str, Any]:
    settings = get_settings()
    return {
        "version": _CACHE_VERSION,
        "audio_sha256": _compute_audio_sha256(audio_path),
        "audio_size": audio_path.stat().st_size,
        "base_url": _normalize_base_url(settings.xfyun_asr_base_url),
        "app_id": settings.xfyun_asr_app_id.strip(),
        "language": settings.xfyun_asr_language.strip() or "autodialect",
        "domain": settings.xfyun_asr_domain.strip(),
        "role_type": max(settings.xfyun_asr_role_type, 0),
        "role_num": max(settings.xfyun_asr_role_num, 0),
        "duration_check_disable": bool(settings.xfyun_asr_duration_check_disable),
        "eng_smoothproc": bool(settings.xfyun_asr_eng_smoothproc),
        "eng_colloqproc": bool(settings.xfyun_asr_eng_colloqproc),
    }


def _cache_key_from_snapshot(snapshot: dict[str, Any]) -> str:
    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _cache_paths(audio_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    snapshot = _cache_settings_snapshot(audio_path)
    cache_key = _cache_key_from_snapshot(snapshot)
    root = _cache_root()
    return root / f"{cache_key}.json", root / f"{cache_key}.lock", snapshot


def _load_cached_transcription(cache_path: Path) -> tuple[list[dict[str, Any]], str, int] | None:
    if not cache_path.is_file():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("XFYUN ASR cache read failed for %s: %s", cache_path.name, exc)
        return None

    if not isinstance(payload, dict):
        return None

    utterances = payload.get("utterances")
    if not isinstance(utterances, list):
        return None
    full_text = str(payload.get("full_text") or "").strip()
    duration_ms = _coerce_int(payload.get("duration_ms")) or 0
    return utterances, full_text, duration_ms


def _write_cached_transcription(
    cache_path: Path,
    snapshot: dict[str, Any],
    utterances: list[dict[str, Any]],
    full_text: str,
    duration_ms: int,
    *,
    order_id: str | None,
) -> None:
    payload = {
        "cache_version": _CACHE_VERSION,
        "cached_at": _now_string(),
        "provider": "xfyun_asr",
        "params": snapshot,
        "order_id": order_id,
        "utterances": utterances,
        "full_text": full_text,
        "duration_ms": duration_ms,
    }
    temp_path = cache_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(cache_path)


async def _acquire_cache_lock(lock_path: Path) -> int:
    stale_after_seconds = max(int(get_settings().xfyun_asr_timeout_seconds) + 300, 600)
    deadline = time.monotonic() + stale_after_seconds
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, f"{os.getpid()} {time.time():.0f}\n".encode("utf-8"))
            return fd
        except FileExistsError:
            try:
                lock_age_seconds = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if lock_age_seconds > stale_after_seconds:
                lock_path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"等待讯飞 ASR 缓存锁超时：{lock_path.name}")
            await asyncio.sleep(1)


def _release_cache_lock(lock_path: Path, fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    lock_path.unlink(missing_ok=True)


async def _iter_file_bytes(path: Path, chunk_size: int = 1024 * 1024):
    with path.open("rb") as file_obj:
        while True:
            chunk = file_obj.read(chunk_size)
            if not chunk:
                break
            yield chunk


def _build_upload_query_params(audio_path: Path) -> tuple[dict[str, str], str]:
    settings = get_settings()
    file_size = audio_path.stat().st_size
    if file_size <= 0:
        raise XfyunAsrError(f"讯飞 ASR 不支持空文件：{audio_path.name}")
    if file_size > _MAX_FILE_SIZE_BYTES:
        raise XfyunAsrError(f"讯飞 ASR 文件大小超限（>{_MAX_FILE_SIZE_BYTES} bytes）：{audio_path.name}")

    signature_random = _generate_signature_random()
    params: dict[str, str] = {
        "appId": settings.xfyun_asr_app_id.strip(),
        "accessKeyId": settings.xfyun_asr_access_key_id.strip(),
        "dateTime": _now_string(),
        "signatureRandom": signature_random,
        "fileSize": str(file_size),
        "fileName": audio_path.name,
        "language": settings.xfyun_asr_language.strip() or "autodialect",
        "durationCheckDisable": _stringify_bool(settings.xfyun_asr_duration_check_disable),
        "eng_smoothproc": _stringify_bool(settings.xfyun_asr_eng_smoothproc),
        "eng_colloqproc": _stringify_bool(settings.xfyun_asr_eng_colloqproc),
    }
    if settings.xfyun_asr_domain.strip():
        params["pd"] = settings.xfyun_asr_domain.strip()
    role_type = max(settings.xfyun_asr_role_type, 0)
    if role_type:
        params["roleType"] = str(role_type)
        if settings.xfyun_asr_role_num > 0:
            params["roleNum"] = str(settings.xfyun_asr_role_num)
    return params, signature_random


def _assert_success_response(payload: dict[str, Any], *, action: str) -> dict[str, Any]:
    code = str(payload.get("code") or "")
    if code != "000000":
        desc = str(payload.get("descInfo") or "unknown error")
        raise XfyunAsrError(f"讯飞 ASR {action} 失败：code={code} desc={desc}")
    content = payload.get("content")
    if not isinstance(content, dict):
        raise XfyunAsrError(f"讯飞 ASR {action} 返回缺少 content")
    return content


async def _upload_audio(audio_path: Path) -> tuple[str, str]:
    settings = get_settings()
    params, signature_random = _build_upload_query_params(audio_path)
    signature = _build_signature(params, settings.xfyun_asr_access_key_secret.strip())
    url = f"{_normalize_base_url(settings.xfyun_asr_base_url)}/v2/upload"

    client = await _get_shared_client()
    response = await client.post(
        url,
        params=params,
        headers={
            "Content-Type": "application/octet-stream",
            "signature": signature,
        },
        content=_iter_file_bytes(audio_path),
    )
    response.raise_for_status()
    payload = response.json()

    content = _assert_success_response(payload, action="upload")
    order_id = str(content.get("orderId") or "").strip()
    if not order_id:
        raise XfyunAsrError("讯飞 ASR upload 未返回 orderId")
    return order_id, signature_random


async def _get_result(order_id: str, signature_random: str) -> dict[str, Any]:
    settings = get_settings()
    params = {
        "accessKeyId": settings.xfyun_asr_access_key_id.strip(),
        "dateTime": _now_string(),
        "signatureRandom": signature_random,
        "orderId": order_id,
        "resultType": "transfer",
    }
    signature = _build_signature(params, settings.xfyun_asr_access_key_secret.strip())
    url = f"{_normalize_base_url(settings.xfyun_asr_base_url)}/v2/getResult"

    client = await _get_shared_client()
    response = await client.post(
        url,
        params=params,
        headers={
            "Content-Type": "application/json",
            "signature": signature,
        },
        json={},
    )
    response.raise_for_status()
    payload = response.json()

    content = _assert_success_response(payload, action="getResult")
    return content


async def _wait_for_result(order_id: str, signature_random: str) -> dict[str, Any]:
    settings = get_settings()
    deadline = time.monotonic() + max(settings.xfyun_asr_timeout_seconds, 60)
    last_status: int | None = None

    while True:
        content = await _get_result(order_id, signature_random)
        order_info = content.get("orderInfo") or {}
        status = _coerce_int(order_info.get("status"))
        fail_type = _coerce_int(order_info.get("failType")) or 0

        if status != last_status:
            logger.info("XFYUN ASR order %s status=%s failType=%s", order_id, status, fail_type)
            last_status = status

        if status == 4:
            return content
        if status == -1:
            fail_label = _FAIL_TYPE_LABELS.get(fail_type, "未知异常")
            raise XfyunAsrError(f"讯飞 ASR 任务失败：failType={fail_type}({fail_label}) orderId={order_id}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"讯飞 ASR 任务超时：orderId={order_id}")

        await asyncio.sleep(max(settings.xfyun_asr_poll_interval_seconds, 1))


def parse_xfyun_order_result(
    order_result: str | dict[str, Any] | None,
    *,
    original_duration_ms: int | None = None,
) -> tuple[list[dict[str, Any]], str, int]:
    if not order_result:
        duration_ms = max(original_duration_ms or 0, 0)
        return [], "", duration_ms

    if isinstance(order_result, str):
        payload = json.loads(order_result)
    elif isinstance(order_result, dict):
        payload = order_result
    else:
        raise XfyunAsrError("讯飞 ASR orderResult 结构无效")

    lattice_items = payload.get("lattice") or []
    utterances: list[dict[str, Any]] = []

    for item in lattice_items:
        if not isinstance(item, dict):
            continue
        onebest_raw = item.get("json_1best")
        if isinstance(onebest_raw, str):
            onebest = json.loads(onebest_raw)
        elif isinstance(onebest_raw, dict):
            onebest = onebest_raw
        else:
            continue

        st = onebest.get("st") or {}
        begin_ms = max(_coerce_int(st.get("bg")) or 0, 0)
        end_ms = max(_coerce_int(st.get("ed")) or begin_ms, begin_ms)
        speaker_id = _normalize_speaker_label(st.get("rl"))

        text_parts: list[str] = []
        for rt in st.get("rt") or []:
            if not isinstance(rt, dict):
                continue
            for ws in rt.get("ws") or []:
                if not isinstance(ws, dict):
                    continue
                candidates = ws.get("cw") or []
                if not candidates:
                    continue
                candidate = candidates[0]
                if not isinstance(candidate, dict):
                    continue
                word = str(candidate.get("w") or "")
                wp = str(candidate.get("wp") or "")
                if wp == "g":
                    continue
                text_parts.append(word)

        text = "".join(text_parts).strip()
        if not text:
            continue
        utterances.append(
            {
                "speaker": speaker_id,
                "speaker_id": speaker_id,
                "speaker_role_source": "xfyun_asr",
                "text": text,
                "begin_ms": begin_ms,
                "end_ms": end_ms,
            }
        )

    full_text = " ".join(str(item.get("text") or "") for item in utterances).strip()
    duration_ms = utterances[-1]["end_ms"] if utterances else max(original_duration_ms or 0, 0)
    return utterances, full_text, duration_ms


async def transcribe_audio(audio_path: str | Path) -> tuple[list[dict[str, Any]], str, int]:
    _validate_runtime_prerequisites()

    resolved_path = Path(audio_path).resolve()
    cache_path, lock_path, cache_snapshot = _cache_paths(resolved_path)
    cached_result = _load_cached_transcription(cache_path)
    if cached_result is not None:
        logger.info("XFYUN ASR cache hit for %s", resolved_path.name)
        return cached_result

    lock_fd: int | None = None
    lock_fd = await _acquire_cache_lock(lock_path)
    try:
        cached_result = _load_cached_transcription(cache_path)
        if cached_result is not None:
            logger.info("XFYUN ASR cache hit after lock for %s", resolved_path.name)
            return cached_result

        order_id, signature_random = await _upload_audio(resolved_path)
        result_content = await _wait_for_result(order_id, signature_random)
        order_info = result_content.get("orderInfo") or {}
        utterances, full_text, duration_ms = parse_xfyun_order_result(
            result_content.get("orderResult"),
            original_duration_ms=_coerce_int(order_info.get("originalDuration")),
        )
        _write_cached_transcription(
            cache_path,
            cache_snapshot,
            utterances,
            full_text,
            duration_ms,
            order_id=order_id,
        )
        logger.info(
            "XFYUN ASR completed for %s: order_id=%s utterances=%d duration_ms=%d",
            resolved_path.name,
            order_id,
            len(utterances),
            duration_ms,
        )
        return utterances, full_text, duration_ms
    finally:
        _release_cache_lock(lock_path, lock_fd)
