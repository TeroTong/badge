from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from smart_badge_api.asr.tencent_media_proxy import build_tencent_media_url
from smart_badge_api.asr.tencent_request_audit import append_tencent_request_event
from smart_badge_api.asr.tencent_task_registry import (
    acquire_tencent_submit_lock,
    get_tencent_task_registry_entry,
    release_tencent_submit_lock,
    upsert_tencent_task_registry_entry,
)
from smart_badge_api.core.config import get_settings


# 共享 httpx AsyncClient（连接池复用）。
_HTTP_CLIENT: httpx.AsyncClient | None = None
_HTTP_CLIENT_LOCK: asyncio.Lock | None = None
_HOTWORD_VOCAB_CACHE: dict[str, Any] | None = None
_HOTWORD_VOCAB_CACHE_LOCK: asyncio.Lock | None = None
_TENCENT_HOTWORD_VOCAB_MAX_WORDS = 1000
_TENCENT_HOTWORD_MAX_BYTES = 30


async def _get_shared_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT, _HTTP_CLIENT_LOCK
    if _HTTP_CLIENT_LOCK is None:
        _HTTP_CLIENT_LOCK = asyncio.Lock()
    if _HTTP_CLIENT is not None and not _HTTP_CLIENT.is_closed:
        return _HTTP_CLIENT
    async with _HTTP_CLIENT_LOCK:
        if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
            limits = httpx.Limits(max_keepalive_connections=10, max_connections=30, keepalive_expiry=30.0)
            _HTTP_CLIENT = httpx.AsyncClient(timeout=30.0, limits=limits)
        return _HTTP_CLIENT


async def close_shared_tencent_client() -> None:
    global _HTTP_CLIENT
    client = _HTTP_CLIENT
    _HTTP_CLIENT = None
    if client is not None and not client.is_closed:
        await client.aclose()

logger = logging.getLogger(__name__)

_RESULT_TIMESTAMP_PATTERN = re.compile(r"\[\d+:\d+\.\d+,\d+:\d+\.\d+\]\s*")
_REQUEST_ID_PATTERN = re.compile(r"request_id=([0-9a-fA-F-]+)")
_ERROR_CODE_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+):")
_SILENCE_START_PATTERN = re.compile(r"silence_start:\s*(\d+(?:\.\d+)?)")
_SILENCE_END_PATTERN = re.compile(r"silence_end:\s*(\d+(?:\.\d+)?)")
_FFMPEG_DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_MIN_SILENCE_AWARE_CHUNK_SECONDS = 30


class TencentAsrError(RuntimeError):
    """腾讯云 ASR 调用失败。"""


@dataclass(slots=True)
class _DirectUploadChunk:
    name: str
    data: bytes | None
    duration_ms: int
    url: str | None = None
    file_size_bytes: int | None = None


@dataclass(slots=True)
class _SilenceSpan:
    start_seconds: float
    end_seconds: float

    @property
    def midpoint_seconds(self) -> float:
        return (self.start_seconds + self.end_seconds) / 2

    @property
    def duration_seconds(self) -> float:
        return max(self.end_seconds - self.start_seconds, 0.0)


_FFMPEG_EXECUTABLE: str | None = None
_DIRECT_UPLOAD_MIN_BITRATE_KBPS = 40


def _normalize_endpoint_host(value: str) -> str:
    text = value.strip()
    if not text:
        return "asr.tencentcloudapi.com"
    parsed = urlparse(text if "://" in text else f"https://{text}")
    return parsed.netloc or parsed.path


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _tc3_signature(
    *,
    host: str,
    payload: str,
    action: str,
    region: str,
    secret_id: str,
    secret_key: str,
    timestamp: int,
) -> str:
    date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
    canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{host}\n"
    signed_headers = "content-type;host"
    canonical_request = "\n".join([
        "POST",
        "/",
        "",
        canonical_headers,
        signed_headers,
        _sha256_hex(payload),
    ])
    credential_scope = f"{date}/asr/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256",
        str(timestamp),
        credential_scope,
        _sha256_hex(canonical_request),
    ])

    secret_date = _sign(f"TC3{secret_key}".encode("utf-8"), date)
    secret_service = _sign(secret_date, "asr")
    secret_signing = _sign(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    return (
        "TC3-HMAC-SHA256 "
        f"Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )


def _validate_runtime_prerequisites() -> None:
    settings = get_settings()
    if not settings.tencent_asr_secret_id.strip() or not settings.tencent_asr_secret_key.strip():
        raise TencentAsrError("未配置腾讯云 ASR 凭证，请设置 TENCENT_ASR_SECRET_ID / TENCENT_ASR_SECRET_KEY")


async def _call_tencent_api(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    return await _call_tencent_api_with_options(action, payload, region_override=None, endpoint_override=None)


async def _call_tencent_api_with_options(
    action: str,
    payload: dict[str, Any],
    *,
    region_override: str | None,
    endpoint_override: str | None,
) -> dict[str, Any]:
    settings = get_settings()
    host = _normalize_endpoint_host(endpoint_override or settings.tencent_asr_endpoint)
    url = f"https://{host}/"
    timestamp = int(time.time())
    payload_raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    resolved_region = (region_override or settings.tencent_asr_region).strip()

    headers = {
        "Authorization": _tc3_signature(
            host=host,
            payload=payload_raw,
            action=action,
            region=resolved_region,
            secret_id=settings.tencent_asr_secret_id.strip(),
            secret_key=settings.tencent_asr_secret_key.strip(),
            timestamp=timestamp,
        ),
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Version": "2019-06-14",
        "X-TC-Timestamp": str(timestamp),
    }
    if resolved_region:
        headers["X-TC-Region"] = resolved_region
    if settings.tencent_asr_session_token.strip():
        headers["X-TC-Token"] = settings.tencent_asr_session_token.strip()

    # These actions are NOT idempotent: retrying can create duplicated tasks or
    # duplicated vocabularies if the first request succeeded but the response was lost.
    _NON_IDEMPOTENT_ACTIONS = {"CreateRecTask", "CreateAsrVocab"}
    max_attempts = 1 if action in _NON_IDEMPOTENT_ACTIONS else 4

    body: dict[str, Any] | None = None
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            client = await _get_shared_client()
            response = await client.post(url, content=payload_raw.encode("utf-8"), headers=headers)
            response.raise_for_status()
            body = response.json()
            break
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt >= max_attempts:
                raise TencentAsrError(f"腾讯云 ASR 请求失败：{action}") from exc
            delay_seconds = min(2 ** (attempt - 1), 8)
            logger.warning(
                "Tencent ASR request failed action=%s attempt=%d/%d: %s; retrying in %ss",
                action,
                attempt,
                max_attempts,
                exc,
                delay_seconds,
            )
            await asyncio.sleep(delay_seconds)

    if body is None:
        raise TencentAsrError(f"腾讯云 ASR 请求失败：{action}") from last_error

    response_payload = body.get("Response") or {}
    error = response_payload.get("Error")
    if error:
        code = str(error.get("Code") or "TencentAsrError")
        message = str(error.get("Message") or "unknown error")
        request_id = str(response_payload.get("RequestId") or "").strip()
        suffix = f" request_id={request_id}" if request_id else ""
        raise TencentAsrError(f"{code}: {message}{suffix}")
    return response_payload


def _normalize_speaker_label(value: object) -> str:
    if value is None:
        return "unknown"
    text = str(value).strip()
    if not text:
        return "unknown"
    return f"speaker_{text}" if text.isdigit() else text


def _clean_result_text(value: object) -> str:
    text = str(value or "")
    text = _RESULT_TIMESTAMP_PATTERN.sub("", text)
    text = text.replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


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


def _normalize_hotword_word_weights(word_weights: list[dict[str, object]] | None) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in word_weights or []:
        if not isinstance(item, dict):
            continue
        term = re.sub(r"\s+", "", str(item.get("Word") or item.get("word") or "").strip())
        if not term or len(term.encode("utf-8")) > _TENCENT_HOTWORD_MAX_BYTES:
            continue
        if any(unicodedata.category(char).startswith(("P", "S")) for char in term):
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)

        weight = _coerce_int(item.get("Weight") or item.get("weight")) or 10
        if weight >= 100:
            resolved_weight = 100
        else:
            resolved_weight = min(max(weight, 1), 11)
        normalized.append({"Word": term, "Weight": resolved_weight})
        if len(normalized) >= _TENCENT_HOTWORD_VOCAB_MAX_WORDS:
            break
    return normalized


def _hotword_vocab_digest(word_weights: list[dict[str, object]]) -> str:
    payload = json.dumps(word_weights, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _hotword_vocab_cache_path() -> Path:
    return get_settings().asr_runtime_path / "tencent_hotword_vocab.json"


def _load_hotword_vocab_cache() -> dict[str, Any]:
    global _HOTWORD_VOCAB_CACHE
    if _HOTWORD_VOCAB_CACHE is not None:
        return dict(_HOTWORD_VOCAB_CACHE)

    path = _hotword_vocab_cache_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    _HOTWORD_VOCAB_CACHE = payload if isinstance(payload, dict) else {}
    return dict(_HOTWORD_VOCAB_CACHE)


def _save_hotword_vocab_cache(payload: dict[str, Any]) -> None:
    global _HOTWORD_VOCAB_CACHE
    _HOTWORD_VOCAB_CACHE = dict(payload)
    path = _hotword_vocab_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_hotword_vocab_id(response_payload: dict[str, Any]) -> str | None:
    candidates: list[dict[str, Any]] = []
    if isinstance(response_payload, dict):
        candidates.append(response_payload)
        data = response_payload.get("Data")
        if isinstance(data, dict):
            candidates.append(data)
    for container in candidates:
        for key in ("VocabId", "VocabID", "HotwordId", "HotwordID"):
            value = str(container.get(key) or "").strip()
            if value:
                return value
    return None


async def _create_tencent_hotword_vocab(word_weights: list[dict[str, object]]) -> str:
    settings = get_settings()
    response_payload = await _call_tencent_api(
        "CreateAsrVocab",
        {
            "Name": settings.tencent_asr_hotword_vocab_name.strip() or "smart-badge-hotwords",
            "Description": settings.tencent_asr_hotword_vocab_description.strip() or "Smart Badge ASR hotwords",
            "WordWeights": word_weights,
        },
    )
    vocab_id = _extract_hotword_vocab_id(response_payload)
    if not vocab_id:
        raise TencentAsrError("Tencent ASR CreateAsrVocab did not return VocabId")
    return vocab_id


async def _update_tencent_hotword_vocab(
    vocab_id: str,
    word_weights: list[dict[str, object]],
) -> str:
    settings = get_settings()
    await _call_tencent_api(
        "UpdateAsrVocab",
        {
            "VocabId": vocab_id,
            "Name": settings.tencent_asr_hotword_vocab_name.strip() or "smart-badge-hotwords",
            "Description": settings.tencent_asr_hotword_vocab_description.strip() or "Smart Badge ASR hotwords",
            "WordWeights": word_weights,
        },
    )
    return vocab_id


async def _ensure_tencent_hotword_vocab(word_weights: list[dict[str, object]] | None) -> str | None:
    settings = get_settings()
    configured_vocab_id = settings.tencent_asr_hotword_vocab_id.strip()
    if not settings.tencent_asr_hotword_vocab_sync_enabled:
        return configured_vocab_id or None

    normalized_word_weights = _normalize_hotword_word_weights(word_weights)
    if not normalized_word_weights:
        return configured_vocab_id or None

    global _HOTWORD_VOCAB_CACHE_LOCK
    if _HOTWORD_VOCAB_CACHE_LOCK is None:
        _HOTWORD_VOCAB_CACHE_LOCK = asyncio.Lock()

    async with _HOTWORD_VOCAB_CACHE_LOCK:
        digest = _hotword_vocab_digest(normalized_word_weights)
        cache = _load_hotword_vocab_cache()
        cached_vocab_id = str(cache.get("vocab_id") or "").strip()
        cached_digest = str(cache.get("digest") or "").strip()
        cached_name = str(cache.get("name") or "").strip()
        vocab_name = settings.tencent_asr_hotword_vocab_name.strip() or "smart-badge-hotwords"
        vocab_id = configured_vocab_id or cached_vocab_id

        if vocab_id and cached_vocab_id == vocab_id and cached_digest == digest and cached_name == vocab_name:
            return vocab_id

        if vocab_id:
            vocab_id = await _update_tencent_hotword_vocab(vocab_id, normalized_word_weights)
            logger.info("Updated Tencent ASR hotword vocabulary: vocab_id=%s words=%d", vocab_id, len(normalized_word_weights))
        else:
            vocab_id = await _create_tencent_hotword_vocab(normalized_word_weights)
            logger.info("Created Tencent ASR hotword vocabulary: vocab_id=%s words=%d", vocab_id, len(normalized_word_weights))

        _save_hotword_vocab_cache(
            {
                "vocab_id": vocab_id,
                "digest": digest,
                "name": vocab_name,
                "word_count": len(normalized_word_weights),
                "synced_at": datetime.now(UTC).isoformat(),
            }
        )
        return vocab_id


def _build_hotword_list_from_word_weights(word_weights: list[dict[str, object]] | None) -> str | None:
    hotwords = [
        f"{item['Word']}|{item['Weight']}"
        for item in _normalize_hotword_word_weights(word_weights)[:128]
    ]
    return ",".join(hotwords) if hotwords else None


async def _resolve_tencent_hotword_config(
    *,
    hotword_word_weights: list[dict[str, object]] | None,
    hotword_list: str | None,
) -> tuple[str | None, str | None]:
    settings = get_settings()
    normalized_word_weights = _normalize_hotword_word_weights(hotword_word_weights)
    if normalized_word_weights:
        try:
            hotword_id = await _ensure_tencent_hotword_vocab(normalized_word_weights)
        except Exception:
            logger.exception("Failed to sync Tencent ASR hotword vocabulary; falling back to request HotwordList")
            hotword_id = settings.tencent_asr_hotword_vocab_id.strip() or None
        if hotword_id:
            return hotword_id, None
        return None, _build_hotword_list_from_word_weights(normalized_word_weights) or hotword_list

    configured_vocab_id = settings.tencent_asr_hotword_vocab_id.strip()
    if configured_vocab_id:
        return configured_vocab_id, None
    return None, hotword_list


def _apply_tencent_hotword_fields(
    payload: dict[str, Any],
    *,
    hotword_id: str | None,
    hotword_list: str | None,
) -> None:
    resolved_hotword_id = str(hotword_id or "").strip()
    if resolved_hotword_id:
        payload["HotwordId"] = resolved_hotword_id
        return
    resolved_hotword_list = str(hotword_list or "").strip() or get_settings().tencent_asr_hotword_list.strip()
    if resolved_hotword_list:
        payload["HotwordList"] = resolved_hotword_list


def parse_tencent_task_data(data: dict[str, Any]) -> tuple[list[dict], str, int]:
    detail_items = list(data.get("ResultDetail") or [])
    utterances: list[dict] = []

    for item in detail_items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("FinalSentence") or item.get("SliceSentence") or "").strip()
        if not text:
            continue

        begin_ms = max(int(item.get("StartMs") or 0), 0)
        end_ms = max(int(item.get("EndMs") or begin_ms), begin_ms)
        speaker_id = _normalize_speaker_label(item.get("SpeakerId"))
        utterances.append(
            {
                "speaker": speaker_id,
                "speaker_id": speaker_id,
                "speaker_role_source": "tencent_asr",
                "text": text,
                "begin_ms": begin_ms,
                "end_ms": end_ms,
            }
        )

    full_text = " ".join(str(item.get("text") or "") for item in utterances).strip()
    if not full_text:
        full_text = _clean_result_text(data.get("Result"))

    duration_ms = utterances[-1]["end_ms"] if utterances else int(round(float(data.get("AudioDuration") or 0) * 1000))
    if utterances:
        return utterances, full_text, duration_ms

    if full_text:
        return (
            [
                {
                    "speaker": "unknown",
                    "speaker_id": "unknown",
                    "speaker_role_source": "tencent_asr",
                    "text": full_text,
                    "begin_ms": 0,
                    "end_ms": duration_ms,
                }
            ],
            full_text,
            duration_ms,
        )
    return [], "", duration_ms


def _build_create_rec_task_payload_from_bytes(
    audio_bytes: bytes,
    *,
    hotword_id: str | None = None,
    hotword_list: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    payload: dict[str, Any] = {
        "EngineModelType": settings.tencent_asr_engine_model_type,
        "ChannelNum": settings.tencent_asr_channel_num,
        "ResTextFormat": settings.tencent_asr_res_text_format,
        "SourceType": 1,
        "Data": base64.b64encode(audio_bytes).decode("ascii"),
        "DataLen": len(audio_bytes),
        "SpeakerDiarization": settings.tencent_asr_speaker_diarization,
    }
    if settings.tencent_asr_speaker_number > 0:
        payload["SpeakerNumber"] = settings.tencent_asr_speaker_number
    _apply_tencent_hotword_fields(payload, hotword_id=hotword_id, hotword_list=hotword_list)
    if settings.tencent_asr_replace_text_id.strip():
        payload["ReplaceTextId"] = settings.tencent_asr_replace_text_id.strip()
    return payload


def _build_create_rec_task_payload_from_url(
    audio_url: str,
    *,
    hotword_id: str | None = None,
    hotword_list: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    payload: dict[str, Any] = {
        "EngineModelType": settings.tencent_asr_engine_model_type,
        "ChannelNum": settings.tencent_asr_channel_num,
        "ResTextFormat": settings.tencent_asr_res_text_format,
        "SourceType": 0,
        "Url": audio_url,
        "SpeakerDiarization": settings.tencent_asr_speaker_diarization,
    }
    if settings.tencent_asr_speaker_number > 0:
        payload["SpeakerNumber"] = settings.tencent_asr_speaker_number
    _apply_tencent_hotword_fields(payload, hotword_id=hotword_id, hotword_list=hotword_list)
    if settings.tencent_asr_replace_text_id.strip():
        payload["ReplaceTextId"] = settings.tencent_asr_replace_text_id.strip()
    return payload


async def _create_rec_task(payload: dict[str, Any]) -> tuple[int, str | None]:
    response_payload = await _call_tencent_api("CreateRecTask", payload)
    data = response_payload.get("Data") or {}
    task_id = data.get("TaskId")
    if task_id is None:
        raise TencentAsrError("腾讯云 ASR 未返回 TaskId")
    request_id = str(response_payload.get("RequestId") or "").strip() or None
    return int(task_id), request_id


def _extract_request_metadata(exc: Exception) -> tuple[str | None, str | None]:
    message = str(exc or "")
    request_id_match = _REQUEST_ID_PATTERN.search(message)
    error_code_match = _ERROR_CODE_PATTERN.match(message)
    request_id = request_id_match.group(1) if request_id_match else None
    error_code = error_code_match.group(1) if error_code_match else None
    return request_id, error_code


def _registry_entry_blocks_new_submit(entry: dict[str, Any] | None) -> bool:
    if not entry:
        return False
    task_id = _coerce_int(entry.get("task_id"))
    if task_id is not None:
        return False
    status = str(entry.get("status") or "").strip().lower()
    return bool(status)


def _resolve_ffmpeg_executable() -> str:
    global _FFMPEG_EXECUTABLE
    if _FFMPEG_EXECUTABLE:
        return _FFMPEG_EXECUTABLE

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        _FFMPEG_EXECUTABLE = system_ffmpeg
        return _FFMPEG_EXECUTABLE

    try:
        import imageio_ffmpeg

        _FFMPEG_EXECUTABLE = imageio_ffmpeg.get_ffmpeg_exe()
        logger.info("Using bundled imageio-ffmpeg executable for Tencent ASR chunk transcoding: %s", _FFMPEG_EXECUTABLE)
        return _FFMPEG_EXECUTABLE
    except Exception as exc:  # pragma: no cover - exercised by failure path below
        raise FileNotFoundError("ffmpeg") from exc


def _resolve_direct_upload_chunk_bitrate_kbps() -> int:
    settings = get_settings()
    return max(settings.tencent_asr_direct_upload_bitrate_kbps, _DIRECT_UPLOAD_MIN_BITRATE_KBPS)


def _resolve_direct_upload_segment_seconds(*, max_bytes: int, bitrate_kbps: int) -> int:
    settings = get_settings()
    configured_seconds = max(int(settings.tencent_asr_direct_upload_segment_seconds), _MIN_SILENCE_AWARE_CHUNK_SECONDS)
    safe_seconds = int((max_bytes * 8 / max(bitrate_kbps * 1000, 1)) * 0.9)
    if safe_seconds <= 0:
        return configured_seconds
    return max(min(configured_seconds, safe_seconds), _MIN_SILENCE_AWARE_CHUNK_SECONDS)


def _chunk_file_size_bytes(chunk: Any) -> int:
    explicit_size = getattr(chunk, "file_size_bytes", None)
    if isinstance(explicit_size, int) and explicit_size >= 0:
        return explicit_size
    data = getattr(chunk, "data", None)
    return len(data) if isinstance(data, (bytes, bytearray)) else 0


def _build_create_rec_task_payload_from_chunk(
    chunk: Any,
    *,
    hotword_id: str | None = None,
    hotword_list: str | None = None,
) -> dict[str, Any]:
    audio_url = str(getattr(chunk, "url", "") or "").strip()
    if audio_url:
        return _build_create_rec_task_payload_from_url(
            audio_url,
            hotword_id=hotword_id,
            hotword_list=hotword_list,
        )
    data = getattr(chunk, "data", None)
    if not isinstance(data, (bytes, bytearray)):
        raise TencentAsrError("腾讯云 ASR 直传分片缺少音频数据")
    return _build_create_rec_task_payload_from_bytes(
        bytes(data),
        hotword_id=hotword_id,
        hotword_list=hotword_list,
    )


def _probe_audio_duration_ms(audio_path: Path) -> int:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return _probe_audio_duration_ms_with_ffmpeg(audio_path)

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        logger.warning("ffprobe not found, skipping duration probe for %s", audio_path.name)
        return 0
    except subprocess.CalledProcessError:
        logger.warning("ffprobe failed, skipping duration probe for %s", audio_path.name)
        return 0

    text = result.stdout.strip()
    try:
        duration_seconds = float(text or 0.0)
    except ValueError:
        logger.warning("ffprobe returned invalid duration for %s", audio_path.name)
        return 0
    return max(int(round(duration_seconds * 1000)), 0)


def _probe_audio_duration_ms_with_ffmpeg(audio_path: Path) -> int:
    command = [
        _resolve_ffmpeg_executable(),
        "-hide_banner",
        "-i",
        str(audio_path),
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        logger.warning("ffmpeg not found, skipping duration probe for %s", audio_path.name)
        return 0
    output = f"{result.stdout}\n{result.stderr}"
    match = _FFMPEG_DURATION_PATTERN.search(output)
    if not match:
        logger.warning("ffmpeg returned no duration for %s", audio_path.name)
        return 0
    hours = int(match.group(1))
    minutes = int(match.group(2))
    seconds = float(match.group(3))
    return max(int(round((hours * 3600 + minutes * 60 + seconds) * 1000)), 0)


def _detect_silence_spans(audio_path: Path) -> list[_SilenceSpan]:
    settings = get_settings()
    if not settings.tencent_asr_silence_split_enabled:
        return []

    command = [
        _resolve_ffmpeg_executable(),
        "-hide_banner",
        "-nostats",
        "-i",
        str(audio_path),
        "-af",
        (
            "silencedetect="
            f"noise={settings.tencent_asr_silence_split_noise_db}dB:"
            f"d={max(settings.tencent_asr_silence_split_min_duration_seconds, 0.1)}"
        ),
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        logger.warning("Tencent ASR silence detection failed for %s: %s", audio_path.name, exc)
        return []

    spans: list[_SilenceSpan] = []
    pending_start: float | None = None
    output = f"{result.stdout}\n{result.stderr}"
    for line in output.splitlines():
        start_match = _SILENCE_START_PATTERN.search(line)
        if start_match:
            pending_start = float(start_match.group(1))
            continue
        end_match = _SILENCE_END_PATTERN.search(line)
        if end_match and pending_start is not None:
            end_seconds = float(end_match.group(1))
            if end_seconds > pending_start:
                spans.append(_SilenceSpan(pending_start, end_seconds))
            pending_start = None
    return spans


def _choose_silence_aware_cut_points(
    *,
    duration_seconds: float,
    segment_seconds: int,
    silence_spans: list[_SilenceSpan],
    search_window_seconds: int,
) -> list[float]:
    if duration_seconds <= segment_seconds:
        return []

    cuts: list[float] = []
    last_cut = 0.0
    target = float(segment_seconds)
    min_chunk_seconds = min(max(_MIN_SILENCE_AWARE_CHUNK_SECONDS, segment_seconds * 0.25), 180.0)

    while target < duration_seconds - min_chunk_seconds:
        lower = max(last_cut + min_chunk_seconds, target - search_window_seconds)
        upper = min(duration_seconds - min_chunk_seconds, target + search_window_seconds)
        candidates = [
            span
            for span in silence_spans
            if lower <= span.midpoint_seconds <= upper
        ]
        if candidates:
            selected = min(
                candidates,
                key=lambda span: (abs(span.midpoint_seconds - target), -span.duration_seconds),
            )
            cut = selected.midpoint_seconds
        else:
            cut = target

        if cut <= last_cut + 1:
            break
        cuts.append(round(cut, 3))
        last_cut = cut
        target = cut + segment_seconds

    return cuts


def _build_silence_aware_chunk_ranges(audio_path: Path, *, duration_ms: int, segment_seconds: int) -> list[tuple[float, float]]:
    duration_seconds = max(duration_ms / 1000, 0.0)
    settings = get_settings()
    silence_spans = _detect_silence_spans(audio_path)
    cut_points = _choose_silence_aware_cut_points(
        duration_seconds=duration_seconds,
        segment_seconds=segment_seconds,
        silence_spans=silence_spans,
        search_window_seconds=max(settings.tencent_asr_silence_split_window_seconds, 0),
    )
    boundaries = [0.0, *cut_points, duration_seconds]
    ranges = [
        (boundaries[index], boundaries[index + 1])
        for index in range(len(boundaries) - 1)
        if boundaries[index + 1] > boundaries[index]
    ]
    logger.info(
        "Tencent ASR silence-aware chunk ranges for %s: duration_ms=%d segment_seconds=%d silences=%d chunks=%d",
        audio_path.name,
        duration_ms,
        segment_seconds,
        len(silence_spans),
        len(ranges),
    )
    return ranges


def _transcode_direct_upload_chunk(
    *,
    audio_path: Path,
    output_path: Path,
    start_seconds: float,
    end_seconds: float,
    bitrate_kbps: int,
) -> None:
    duration_seconds = max(end_seconds - start_seconds, 0.001)
    command = [
        _resolve_ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-t",
        f"{duration_seconds:.3f}",
        "-i",
        str(audio_path),
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        f"{bitrate_kbps}k",
        str(output_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)


def _prepare_direct_upload_chunks(audio_path: Path) -> list[_DirectUploadChunk]:
    settings = get_settings()
    max_bytes = max(settings.tencent_asr_direct_upload_max_bytes, 1_000_000)

    raw = audio_path.read_bytes()
    if len(raw) <= max_bytes:
        return [
            _DirectUploadChunk(
                name=audio_path.name,
                data=raw,
                duration_ms=0,
                file_size_bytes=len(raw),
            )
        ]

    if settings.tencent_asr_url_upload_enabled:
        try:
            return [
                _DirectUploadChunk(
                    name=audio_path.name,
                    data=None,
                    duration_ms=_probe_audio_duration_ms(audio_path),
                    url=build_tencent_media_url(audio_path, filename=audio_path.name),
                    file_size_bytes=len(raw),
                )
            ]
        except ValueError as exc:
            logger.warning(
                "Tencent ASR URL upload unavailable for %s, falling back to chunk transcoding: %s",
                audio_path,
                exc,
            )

    bitrate_kbps = _resolve_direct_upload_chunk_bitrate_kbps()
    segment_seconds = _resolve_direct_upload_segment_seconds(max_bytes=max_bytes, bitrate_kbps=bitrate_kbps)
    duration_ms = _probe_audio_duration_ms(audio_path)

    with tempfile.TemporaryDirectory(prefix="tencent-asr-") as temp_dir:
        temp_path = Path(temp_dir)
        if duration_ms > 0:
            chunk_ranges = _build_silence_aware_chunk_ranges(
                audio_path,
                duration_ms=duration_ms,
                segment_seconds=segment_seconds,
            )
            chunk_paths: list[Path] = []
            try:
                for index, (start_seconds, end_seconds) in enumerate(chunk_ranges):
                    chunk_path = temp_path / f"chunk_{index:03d}.mp3"
                    _transcode_direct_upload_chunk(
                        audio_path=audio_path,
                        output_path=chunk_path,
                        start_seconds=start_seconds,
                        end_seconds=end_seconds,
                        bitrate_kbps=bitrate_kbps,
                    )
                    chunk_paths.append(chunk_path)
            except (FileNotFoundError, subprocess.CalledProcessError) as exc:
                raise TencentAsrError(f"腾讯云 ASR 静音分片转码失败：{audio_path.name}") from exc
        else:
            output_pattern = temp_path / "chunk_%03d.mp3"
            command = [
                _resolve_ffmpeg_executable(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(audio_path),
                "-vn",
                "-sn",
                "-dn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                f"{bitrate_kbps}k",
                "-f",
                "segment",
                "-segment_time",
                str(segment_seconds),
                "-reset_timestamps",
                "1",
                str(output_pattern),
            ]
            try:
                subprocess.run(command, check=True, capture_output=True, text=True)
            except (FileNotFoundError, subprocess.CalledProcessError) as exc:
                raise TencentAsrError(f"腾讯云 ASR 分片转码失败：{audio_path.name}") from exc
            chunk_paths = sorted(temp_path.glob("chunk_*.mp3"))

        if not chunk_paths:
            raise TencentAsrError(f"腾讯云 ASR 分片结果为空：{audio_path.name}")

        chunks: list[_DirectUploadChunk] = []
        for chunk_path in chunk_paths:
            chunk_bytes = chunk_path.read_bytes()
            if len(chunk_bytes) > max_bytes:
                raise TencentAsrError(
                    "腾讯云 ASR 分片后仍超过直传大小限制，请减小 TENCENT_ASR_DIRECT_UPLOAD_SEGMENT_SECONDS"
                )
            chunks.append(
                _DirectUploadChunk(
                    name=chunk_path.name,
                    data=chunk_bytes,
                    duration_ms=_probe_audio_duration_ms(chunk_path),
                    file_size_bytes=len(chunk_bytes),
                )
            )
        return chunks


def _offset_utterances(utterances: list[dict], offset_ms: int) -> list[dict]:
    if offset_ms <= 0:
        return [dict(item) for item in utterances]

    shifted: list[dict] = []
    for item in utterances:
        shifted.append(
            {
                **item,
                "begin_ms": int(item.get("begin_ms") or 0) + offset_ms,
                "end_ms": int(item.get("end_ms") or 0) + offset_ms,
            }
        )
    return shifted


def _distinct_speaker_ids(utterances: list[dict]) -> set[str]:
    speaker_ids: set[str] = set()
    for item in utterances:
        speaker_id = str(item.get("speaker_id") or item.get("speaker") or "").strip()
        if not speaker_id or speaker_id.lower() == "unknown":
            continue
        speaker_ids.add(speaker_id)
    return speaker_ids


def _assign_local_diarization_to_utterances(
    utterances: list[dict],
    diarization_segments: list[object],
) -> list[dict]:
    from smart_badge_api.asr.sensevoice_3dspeaker_provider import _ordered_speakers_for_interval

    def nearest_speaker_for_interval(begin_ms: int, end_ms: int) -> str | None:
        midpoint = (begin_ms + end_ms) / 2
        best_speaker: str | None = None
        best_distance: float | None = None
        for segment in diarization_segments:
            seg_begin = int(getattr(segment, "begin_ms", 0))
            seg_end = int(getattr(segment, "end_ms", seg_begin))
            if seg_begin <= midpoint <= seg_end:
                return str(getattr(segment, "speaker_id", "")).strip() or None
            if midpoint < seg_begin:
                distance = seg_begin - midpoint
            else:
                distance = midpoint - seg_end
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_speaker = str(getattr(segment, "speaker_id", "")).strip() or None
        return best_speaker

    assigned: list[dict] = []
    for item in utterances:
        clone = dict(item)
        begin_ms = max(int(clone.get("begin_ms") or 0), 0)
        end_ms = max(int(clone.get("end_ms") or begin_ms), begin_ms)
        ordered = _ordered_speakers_for_interval(begin_ms, end_ms, diarization_segments)
        speaker_id = ordered[0] if ordered else nearest_speaker_for_interval(begin_ms, end_ms)
        if speaker_id:
            clone["speaker"] = speaker_id
            clone["speaker_id"] = speaker_id
            clone["speaker_diarization_source"] = "3dspeaker"
        assigned.append(clone)
    return assigned


def _run_local_diarization(audio_path: Path, utterances: list[dict]) -> tuple[list[dict], int]:
    from smart_badge_api.asr.sensevoice_3dspeaker_provider import _get_diarizer, _prepare_audio_path

    prepared_audio_path, cleanup = _prepare_audio_path(audio_path)
    try:
        diarizer = _get_diarizer()
        diarization_segments = diarizer.diarize(prepared_audio_path)
    finally:
        cleanup()

    if not diarization_segments:
        return [dict(item) for item in utterances], 0
    return _assign_local_diarization_to_utterances(utterances, diarization_segments), len(diarization_segments)


async def _maybe_apply_local_diarization(audio_path: Path, utterances: list[dict]) -> list[dict]:
    settings = get_settings()
    if not settings.tencent_asr_local_diarization_enabled or not utterances:
        return utterances

    upstream_speakers = _distinct_speaker_ids(utterances)
    if len(upstream_speakers) > 1:
        return utterances

    loop = asyncio.get_running_loop()
    started_at = time.perf_counter()
    try:
        enriched_utterances, diarization_segment_count = await loop.run_in_executor(
            None,
            _run_local_diarization,
            audio_path,
            utterances,
        )
    except Exception as exc:
        logger.warning("Tencent ASR local diarization failed for %s: %s", audio_path.name, exc)
        return utterances

    local_speakers = _distinct_speaker_ids(enriched_utterances)
    logger.info(
        "Tencent ASR local diarization applied for %s: upstream_speakers=%d local_speakers=%d diar_segments=%d elapsed=%.1fs",
        audio_path.name,
        len(upstream_speakers),
        len(local_speakers),
        diarization_segment_count,
        time.perf_counter() - started_at,
    )
    return enriched_utterances


async def _wait_for_task(task_id: int) -> dict[str, Any]:
    settings = get_settings()
    deadline = time.monotonic() + max(settings.tencent_asr_timeout_seconds, 60)
    last_status: int | None = None

    while True:
        response_payload = await _call_tencent_api("DescribeTaskStatus", {"TaskId": task_id})
        data = response_payload.get("Data") or {}
        status = int(data.get("Status") or 0)
        status_str = str(data.get("StatusStr") or "")

        if status != last_status:
            logger.info("Tencent ASR task %s status=%s(%s)", task_id, status, status_str)
            last_status = status

        if status == 2:
            return data
        if status == 3:
            error_message = str(data.get("ErrorMsg") or "腾讯云 ASR 任务失败")
            raise TencentAsrError(f"Tencent ASR task failed: {error_message}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Tencent ASR task {task_id} timed out after {settings.tencent_asr_timeout_seconds}s")

        await asyncio.sleep(max(settings.tencent_asr_poll_interval_seconds, 1))


async def transcribe_audio(
    audio_path: str | Path,
    *,
    hotword_list: str | None = None,
    hotword_word_weights: list[dict[str, object]] | None = None,
    source_id: str | None = None,
) -> tuple[list[dict], str, int]:
    _validate_runtime_prerequisites()

    resolved_path = Path(audio_path).resolve()
    started_at = time.perf_counter()
    chunks = _prepare_direct_upload_chunks(resolved_path)
    upload_modes = {
        "url" if str(getattr(chunk, "url", "") or "").strip() else "direct"
        for chunk in chunks
    }
    hotword_id, resolved_hotword_list = await _resolve_tencent_hotword_config(
        hotword_word_weights=hotword_word_weights,
        hotword_list=hotword_list,
    )
    logger.info(
        "Submitting Tencent ASR for %s: chunks=%d modes=%s engine=%s diarization=%s hotword_id=%s hotword_list=%s",
        resolved_path,
        len(chunks),
        ",".join(sorted(upload_modes)) or "direct",
        get_settings().tencent_asr_engine_model_type,
        get_settings().tencent_asr_speaker_diarization,
        hotword_id or "-",
        "yes" if resolved_hotword_list else "no",
    )

    utterances: list[dict] = []
    full_text_parts: list[str] = []
    offset_ms = 0

    for index, chunk in enumerate(chunks, start=1):
        chunk_upload_mode = "url" if str(getattr(chunk, "url", "") or "").strip() else "direct"
        chunk_size_bytes = _chunk_file_size_bytes(chunk)
        logger.info(
            "Submitting Tencent ASR chunk %d/%d for %s: name=%s mode=%s size=%d duration_ms=%d",
            index,
            len(chunks),
            resolved_path.name,
            chunk.name,
            chunk_upload_mode,
            chunk_size_bytes,
            chunk.duration_ms,
        )
        request_started_at = datetime.now(UTC)
        request_id: str | None = None
        task_id: int | None = None
        task_error_code: str | None = None
        task_error_message: str | None = None
        task_data: dict[str, Any] | None = None
        chunk_duration_ms: int | None = None
        chunk_utterances: list[dict] = []
        chunk_full_text = ""
        submit_lock_acquired = False
        existing_task = (
            get_tencent_task_registry_entry(
                source_id=source_id,
                chunk_index=index,
                chunk_count=len(chunks),
            )
            if source_id
            else None
        )
        try:
            if existing_task and _coerce_int(existing_task.get("task_id")) is not None:
                task_id = _coerce_int(existing_task.get("task_id"))
                request_id = str(existing_task.get("request_id") or "").strip() or None
                logger.info(
                    "Reusing existing Tencent ASR task for %s chunk %d/%d: task_id=%s request_id=%s status=%s",
                    resolved_path.name,
                    index,
                    len(chunks),
                    task_id,
                    request_id,
                    str(existing_task.get("status") or "").strip() or "unknown",
                )
                if task_id is None:
                    raise TencentAsrError("腾讯云 ASR 任务登记缺少 task_id，无法恢复已提交任务")
            else:
                if _registry_entry_blocks_new_submit(existing_task):
                    raise TencentAsrError("腾讯云 ASR 任务提交状态待确认，已阻止自动重试以避免重复消耗额度")
                if source_id:
                    submit_lock_acquired = await acquire_tencent_submit_lock(
                        source_id=source_id,
                        chunk_index=index,
                        chunk_count=len(chunks),
                    )
                    if not submit_lock_acquired:
                        refreshed_task = get_tencent_task_registry_entry(
                            source_id=source_id,
                            chunk_index=index,
                            chunk_count=len(chunks),
                        )
                        if refreshed_task and _coerce_int(refreshed_task.get("task_id")) is not None:
                            task_id = _coerce_int(refreshed_task.get("task_id"))
                            request_id = str(refreshed_task.get("request_id") or "").strip() or None
                            logger.info(
                                "Reusing existing Tencent ASR task after submit lock wait for %s chunk %d/%d: task_id=%s request_id=%s status=%s",
                                resolved_path.name,
                                index,
                                len(chunks),
                                task_id,
                                request_id,
                                str(refreshed_task.get("status") or "").strip() or "unknown",
                            )
                            if task_id is None:
                                raise TencentAsrError("腾讯云 ASR 任务登记缺少 task_id，无法恢复已提交任务")
                        else:
                            raise TencentAsrError("腾讯云 ASR 任务提交锁超时，已阻止自动重试以避免重复消耗额度")
                    else:
                        existing_task = get_tencent_task_registry_entry(
                            source_id=source_id,
                            chunk_index=index,
                            chunk_count=len(chunks),
                        )
                        if existing_task and _coerce_int(existing_task.get("task_id")) is not None:
                            task_id = _coerce_int(existing_task.get("task_id"))
                            request_id = str(existing_task.get("request_id") or "").strip() or None
                            logger.info(
                                "Reusing existing Tencent ASR task after acquiring submit lock for %s chunk %d/%d: task_id=%s request_id=%s status=%s",
                                resolved_path.name,
                                index,
                                len(chunks),
                                task_id,
                                request_id,
                                str(existing_task.get("status") or "").strip() or "unknown",
                            )
                            if task_id is None:
                                raise TencentAsrError("腾讯云 ASR 任务登记缺少 task_id，无法恢复已提交任务")
                        elif _registry_entry_blocks_new_submit(existing_task):
                            raise TencentAsrError("腾讯云 ASR 任务提交状态待确认，已阻止自动重试以避免重复消耗额度")
                        else:
                            await upsert_tencent_task_registry_entry(
                                source_id=source_id,
                                chunk_index=index,
                                chunk_count=len(chunks),
                                audio_name=resolved_path.name,
                                audio_path=str(resolved_path),
                                status="submitting",
                                request_id=None,
                                task_id=None,
                                submitted_duration_ms=chunk.duration_ms or None,
                                recognized_duration_ms=None,
                                error_code=None,
                                error_message=None,
                            )
                if task_id is None:
                    task_id, request_id = await _create_rec_task(
                        _build_create_rec_task_payload_from_chunk(
                            chunk,
                            hotword_id=hotword_id,
                            hotword_list=resolved_hotword_list,
                        )
                    )
                    if source_id:
                        await upsert_tencent_task_registry_entry(
                            source_id=source_id,
                            chunk_index=index,
                            chunk_count=len(chunks),
                            audio_name=resolved_path.name,
                            audio_path=str(resolved_path),
                            status="submitted",
                            request_id=request_id,
                            task_id=task_id,
                            submitted_duration_ms=chunk.duration_ms or None,
                            recognized_duration_ms=None,
                            error_code=None,
                            error_message=None,
                        )
                    await append_tencent_request_event(
                        occurred_at=request_started_at,
                        status="submitted",
                        audio_name=resolved_path.name,
                        audio_path=str(resolved_path),
                        source_id=source_id,
                        chunk_index=index,
                        chunk_count=len(chunks),
                        submitted_duration_ms=chunk.duration_ms or None,
                        recognized_duration_ms=None,
                        file_size_bytes=chunk_size_bytes,
                        request_id=request_id,
                        task_id=task_id,
                        error_code=None,
                        error_message=None,
                    )
            task_data = await _wait_for_task(task_id)
            chunk_utterances, chunk_full_text, chunk_duration_ms = parse_tencent_task_data(task_data)
        except Exception as exc:
            parsed_request_id, parsed_error_code = _extract_request_metadata(exc)
            request_id = request_id or parsed_request_id
            task_error_code = parsed_error_code
            task_error_message = str(exc)
            if source_id:
                await upsert_tencent_task_registry_entry(
                    source_id=source_id,
                    chunk_index=index,
                    chunk_count=len(chunks),
                    audio_name=resolved_path.name,
                    audio_path=str(resolved_path),
                    status="submit_failed" if task_id is None else "task_failed",
                    request_id=request_id,
                    task_id=task_id,
                    submitted_duration_ms=chunk.duration_ms or None,
                    recognized_duration_ms=chunk_duration_ms,
                    error_code=task_error_code,
                    error_message=task_error_message,
                )
            await append_tencent_request_event(
                occurred_at=request_started_at,
                status="submit_failed" if task_id is None else "task_failed",
                audio_name=resolved_path.name,
                audio_path=str(resolved_path),
                source_id=source_id,
                chunk_index=index,
                chunk_count=len(chunks),
                submitted_duration_ms=chunk.duration_ms or None,
                recognized_duration_ms=chunk_duration_ms,
                file_size_bytes=chunk_size_bytes,
                request_id=request_id,
                task_id=task_id,
                error_code=task_error_code,
                error_message=task_error_message,
            )
            raise
        finally:
            if submit_lock_acquired and source_id:
                await release_tencent_submit_lock(
                    source_id=source_id,
                    chunk_index=index,
                    chunk_count=len(chunks),
                )

        await append_tencent_request_event(
            occurred_at=request_started_at,
            status="completed",
            audio_name=resolved_path.name,
            audio_path=str(resolved_path),
            source_id=source_id,
            chunk_index=index,
            chunk_count=len(chunks),
            submitted_duration_ms=None,
            recognized_duration_ms=chunk_duration_ms,
            file_size_bytes=chunk_size_bytes,
            request_id=request_id,
            task_id=task_id,
            error_code=None,
            error_message=None,
        )
        if source_id:
            await upsert_tencent_task_registry_entry(
                source_id=source_id,
                chunk_index=index,
                chunk_count=len(chunks),
                audio_name=resolved_path.name,
                audio_path=str(resolved_path),
                status="completed",
                request_id=request_id,
                task_id=task_id,
                submitted_duration_ms=chunk.duration_ms or None,
                recognized_duration_ms=chunk_duration_ms,
                error_code=None,
                error_message=None,
            )
        if task_data is None:
            raise TencentAsrError("腾讯云 ASR 未返回任务结果")
        utterances.extend(_offset_utterances(chunk_utterances, offset_ms))
        if chunk_full_text:
            full_text_parts.append(chunk_full_text)
        offset_ms += max(chunk.duration_ms, chunk_duration_ms)

    full_text = " ".join(part for part in full_text_parts if part).strip()
    utterances = await _maybe_apply_local_diarization(resolved_path, utterances)
    duration_ms = utterances[-1]["end_ms"] if utterances else offset_ms
    elapsed = time.perf_counter() - started_at
    logger.info(
        "Tencent ASR completed for %s: task_id=%s utterances=%d duration_ms=%d elapsed=%.1fs",
        resolved_path,
        "multi-chunk" if len(chunks) > 1 else "single-chunk",
        len(utterances),
        duration_ms,
        elapsed,
    )
    return utterances, full_text, duration_ms


async def get_usage_totals_by_date_range(
    *,
    start_date: date,
    end_date: date,
    biz_name_list: list[str] | None = None,
) -> dict[str, dict[str, int]]:
    _validate_runtime_prerequisites()
    response_payload = await _call_tencent_api_with_options(
        "GetUsageByDate",
        {
            "BizNameList": biz_name_list or ["asr_rec"],
            "StartDate": start_date.isoformat(),
            "EndDate": end_date.isoformat(),
        },
        region_override="ap-guangzhou",
        endpoint_override=None,
    )
    data = response_payload.get("Data") or {}
    usage_items = list(data.get("UsageByDateInfoList") or [])
    result: dict[str, dict[str, int]] = {}
    for item in usage_items:
        if not isinstance(item, dict):
            continue
        biz_name = str(item.get("BizName") or "").strip()
        if not biz_name:
            continue
        result[biz_name] = {
            "count": max(int(item.get("Count") or 0), 0),
            "duration": max(int(item.get("Duration") or 0), 0),
        }
    return result


async def get_file_recognition_resource_packages() -> dict[str, Any]:
    _validate_runtime_prerequisites()

    async def _fetch_pid_orders(available_type: int) -> list[dict[str, Any]]:
        page = 1
        page_size = 100
        rows: list[dict[str, Any]] = []
        while True:
            response_payload = await _call_tencent_api_with_options(
                "DescribePidOrders",
                {
                    "AvailableType": available_type,
                    "BacktraceLevel": 0,
                    "Page": page,
                    "PageSize": page_size,
                },
                region_override="ap-guangzhou",
                endpoint_override=None,
            )
            page_rows = list(response_payload.get("PidOrders") or [])
            rows.extend(item for item in page_rows if isinstance(item, dict))
            total_count = max(int(response_payload.get("TotalCount") or 0), 0)
            if len(rows) >= total_count or not page_rows:
                break
            page += 1
        return rows

    package_rows: list[dict[str, Any]] = []
    seen_units: set[str] = set()
    for available_type in (1, 2):
        for item in await _fetch_pid_orders(available_type):
            sub_product_code = str(item.get("SubProductCode") or "").strip()
            unit = str(item.get("Unit") or item.get("Uint") or "").strip()
            if sub_product_code != "sp_asr_file_prepay":
                continue
            if unit and unit in seen_units:
                continue
            if unit:
                seen_units.add(unit)
            total_seconds = max(int(float(item.get("TotalNumFloat") or item.get("TotalNum") or 0)), 0)
            remaining_seconds = max(int(float(item.get("RestNumFloat") or item.get("RestNum") or 0)), 0)
            package_rows.append(
                {
                    "name": str(item.get("Name") or "").strip(),
                    "fee_mode": bool(item.get("FeeMode")),
                    "total_seconds": total_seconds,
                    "remaining_seconds": remaining_seconds,
                    "used_seconds": max(total_seconds - remaining_seconds, 0),
                    "effective_time": str(item.get("EffectiveTime") or "").strip() or None,
                    "expiry_time": str(item.get("ExpiryTime") or "").strip() or None,
                    "pid": _coerce_int(item.get("Pid")),
                    "unit": unit or None,
                    "sub_product_code": sub_product_code,
                    "available_type": available_type,
                }
            )

    total_seconds = sum(int(item["total_seconds"]) for item in package_rows)
    remaining_seconds = sum(int(item["remaining_seconds"]) for item in package_rows)
    used_seconds = max(total_seconds - remaining_seconds, 0)
    exhausted_package_count = sum(1 for item in package_rows if int(item["remaining_seconds"]) <= 0)
    active_package_count = sum(1 for item in package_rows if int(item["remaining_seconds"]) > 0)
    package_rows.sort(
        key=lambda item: (
            0 if item["remaining_seconds"] > 0 else 1,
            item["expiry_time"] or "",
            item["name"] or "",
        )
    )
    return {
        "total_seconds": total_seconds,
        "remaining_seconds": remaining_seconds,
        "used_seconds": used_seconds,
        "package_count": len(package_rows),
        "active_package_count": active_package_count,
        "exhausted_package_count": exhausted_package_count,
        "packages": package_rows,
    }
